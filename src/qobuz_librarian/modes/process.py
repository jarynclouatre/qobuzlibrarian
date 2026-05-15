"""Core album processing — detect gaps, prompt, download, import, consolidate.

"""
import shutil
from datetime import datetime, timezone
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.download import run_album_download
from qobuz_librarian.integrations.beets import (
    _merge_split_folder,
    beets_import_paths,
    staging_preflight,
)
from qobuz_librarian.integrations.lyrics import (
    _record_post_import_lyric_retry,
    _resolve_signatures_to_paths,
    write_post_import_sidecars,
)
from qobuz_librarian.integrations.rip import (
    files_added_since,
    is_cancel_requested,
    snapshot_staging,
)
from qobuz_librarian.library.backup import (
    backup_album_dir,
    restore_gap_fill_backup,
    restore_upgrade_backup,
)
from qobuz_librarian.library.catalog import (
    _is_split_album_merge,
    album_quality_label,
    cleanup_duplicate_art,
    compute_missing,
    find_album_dir_filesystem,
    find_existing_tracks,
    find_expanded_edition,
    find_extras_in_existing,
    is_lossless_album,
    prompt_and_migrate_multi_artist_folder,
)
from qobuz_librarian.library.scanner import clear_scan_caches, read_album_dir
from qobuz_librarian.library.tags import normalize, strip_edition_suffix
from qobuz_librarian.modes.consolidate import consolidate_albums
from qobuz_librarian.quality.decision import (
    compare_album_quality,
    existing_track_quality,
)
from qobuz_librarian.queue.executor import _pre_import_staging_hooks
from qobuz_librarian.ui_cli.colors import C, fmt, format_size, section, truncate
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import (
    confirm,
    log_fetch,
    print_album_summary,
    prompt_edition_pick,
)


def force_cleanup_preflight(album, args):
    """With --force, move an existing album dir aside to a backup before
    re-import — so beets doesn't make '<n>.1.flac' collisions against the old
    files, and a re-download that fails can be restored.

    Returns the backup Path when the folder was moved aside; True when there's
    nothing to move; None for a symlinked dir (skip without further prompts);
    False when the user declined or the backup couldn't be made. --yes does
    NOT silence this prompt."""
    if not args.force:
        return True

    album_dir = find_album_dir_filesystem(album)
    if not album_dir or not album_dir.exists():
        return True

    # Refuse to --force a symlinked album dir: the redownload would land files
    # through the surviving link into the target, wiping the original without
    # an undo. Signal None so the caller skips without a collision prompt (the
    # collision prompt makes no sense here).
    if album_dir.is_symlink():
        log.info("")
        log.info(fmt(C.RED + C.BOLD,
            "  ✗  --force on a symlinked album folder is unsafe:"))
        log.info(fmt(C.WHITE, f"     {album_dir}"))
        log.info(fmt(C.GRAY,
            "     Resolve the symlink (or pick the real path) and re-run."))
        return None

    total_size = 0
    audio_files = []
    for f in album_dir.rglob("*"):
        if f.is_file():
            try:
                total_size += f.stat().st_size
            except OSError:
                pass
            if f.suffix.lower() in cfg.AUDIO_EXTS:
                audio_files.append(f)

    log.info("")
    log.info(fmt(C.YELLOW + C.BOLD, "  ⚠  --force AND existing album folder detected:"))
    log.info(fmt(C.WHITE, f"     {album_dir}"))
    log.info(fmt(C.GRAY,
        f"     {len(audio_files)} audio file(s), {format_size(total_size)} total"))
    log.info(fmt(C.GRAY,
        "     Left in place, beets would create '<n>.1.flac' alongside the old files."))

    move_it = confirm(
        "\n  Move this folder to a backup before re-downloading? "
        "(restored automatically if the re-download fails)",
        default_yes=True, auto_yes=False)
    if not move_it:
        log.info(fmt(C.YELLOW, "  Continuing without moving it. Expect file collisions."))
        return False

    backup_path = backup_album_dir(album_dir)
    if backup_path is None:
        log.info(fmt(C.RED,
            "  ✗  Couldn't back up the folder; refusing to remove it. "
            "Expect collisions."))
        return False
    log.info(fmt(C.GREEN,
        "  ⤷  Moved existing folder to a backup (auto-restore on failure)."))
    return backup_path


_UPGRADE_VERIFY_DURATION_RATIO = 0.97


def _audio_count_and_seconds(folder):
    """(audio-track count, total playtime in seconds) for an album folder,
    read through read_album_dir so lengths come from the same reader the rest
    of the app uses. A folder that can't be read returns (0, 0.0)."""
    total = 0.0
    tracks = read_album_dir(folder)
    for t in tracks:
        try:
            total += float(t.get("length") or 0)
        except (TypeError, ValueError):
            pass
    return len(tracks), total


def _upgrade_replacement_verified(album, album_dir, backup_path):
    """True only when the freshly imported album is at least as complete as
    the backed-up original — same-or-more tracks AND same-or-more playtime.

    The success gate clears `flac -t` per file, which proves each file decodes
    but not that the matcher kept every track or that a re-rip didn't land
    short. A dropped track or a truncated-but-decodable one both show up here
    as missing tracks/seconds. Anything that can't be confirmed (folder not
    found, unreadable) returns False, so the caller keeps the backup rather
    than deleting the only full copy."""
    # beets renames the imported folder to its canonical $albumartist/$album,
    # so the post-import dir is often not the one resolved before the download.
    # The subdir listing was cached then, against the old folder name; clear it
    # so the album resolves to the folder beets actually wrote.
    clear_scan_caches()
    post_dir = find_album_dir_filesystem(album)
    if not post_dir or not post_dir.exists():
        return False
    new_n, new_secs = _audio_count_and_seconds(post_dir)
    old_n, old_secs = _audio_count_and_seconds(backup_path)
    if new_n < old_n:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Upgrade landed {new_n} track(s) but the original held "
            f"{old_n} — keeping the backup."))
        return False
    if old_secs > 0 and new_secs < old_secs * _UPGRADE_VERIFY_DURATION_RATIO:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Upgrade playtime {int(new_secs)}s falls short of the "
            f"original {int(old_secs)}s (a track may be truncated) — "
            f"keeping the backup."))
        return False
    return True


def detect_sibling_album_groups(album_dirs):
    """Group album dirs whose names strip to the same bare title.
    Returns [(bare_title, [dirs])] for groups with 2+ members."""
    from qobuz_librarian.library.tags import strip_album_decorations
    groups = {}
    for d in album_dirs:
        bare = normalize(strip_album_decorations(d.name))
        if not bare:
            continue
        groups.setdefault(bare, []).append(d)
    return [(k, v) for k, v in groups.items() if len(v) >= 2]


def sweep_staging_artwork():
    """Remove streamrip's `__artwork/cover-*.jpg` orphan dirs from staging
    after beets has moved the audio out. `beet import` ignores non-audio
    files, so without this sweep every job leaves a cover-image dir behind
    and the next preflight reports a dirty staging area."""
    try:
        roots = list(cfg.STAGING_DIR.rglob("__artwork"))
    except OSError:
        return
    for d in roots:
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


def _discard_staged_since(snapshot):
    """Delete staging files added since `snapshot` (a cancelled job's partial
    output) and prune the directories left empty, so a cancel leaves staging
    as clean as it was before the rip started."""
    try:
        added = files_added_since(snapshot)
    except OSError:
        return
    for f in added:
        try:
            Path(f).unlink()
        except OSError:
            pass
    try:
        descendants = sorted(cfg.STAGING_DIR.rglob("*"), key=lambda p: -len(p.parts))
    except OSError:
        return
    for d in descendants:
        if d.is_dir() and not any(d.iterdir()):
            try:
                d.rmdir()
            except OSError:
                pass


def pick_canonical_sibling(dirs):
    """Most audio files wins; tiebreak on longest name (more decoration =
    usually the more comprehensive edition like Deluxe / Expanded)."""
    def score(d):
        try:
            n = sum(1 for f in d.rglob("*")
                    if f.is_file() and f.suffix.lower() in cfg.AUDIO_EXTS)
        except OSError:
            n = 0
        return (n, len(d.name))
    return max(dirs, key=score)


def _offer_expanded_edition(album, album_dir, existing, extras, token, args):
    """Look for an expanded Qobuz edition that also covers the on-disk extras
    and let the user pick one. Returns (edition, edition_extras, edition_qual)
    for the chosen album, or (None, [], None) when nothing was offered or the
    user declined."""
    cands = find_expanded_edition(album, album_dir, existing, token, args)
    exp, exp_extras = prompt_edition_pick(
        album, len(extras), cands, existing, args, label_prefix="  ")
    if exp is None:
        return None, [], None
    return exp, exp_extras, compare_album_quality(existing, exp)


def process_album(album, args, *, allow_force=True, label=None,
                  already_confirmed=False, upgrade_only=False,
                  token=None, quality=None):
    """End-to-end processing for one Qobuz album: detect → prompt → download →
    cleanup → import → consolidate.

    Parameters:
      album         Qobuz album dict (must include tracks.items)
      args          parsed argparse Namespace
      allow_force   if False, --force is ignored for this album. Used by
                    artist mode so a single forgetful run doesn't wipe every
                    album by an artist.
      label         optional prefix for status output (e.g. "[3/12]")

    Track-by-track downloading is a queue-only contract: this path always
    downloads the whole album in one rip invocation and computes its
    own per-track decisions (around line 600). Callers that need
    one-track-at-a-time isolation (repair) must go through the queue
    builder/executor and set `force_track_by_track`.

    Returns dict with run results (used by artist mode summary).
    Never raises for "this album can't be done"; only KeyboardInterrupt and
    AuthLost propagate.
    """
    use_force = bool(args.force) and allow_force
    label_prefix = (label + " ") if label else ""

    if not is_lossless_album(album):
        log.info(fmt(C.RED,
            f"\n  ✗  {label_prefix}Lossy-only on Qobuz "
            f"({album.get('title') or '?'} — {album_quality_label(album)})."))
        log.info(fmt(C.GRAY, "     Skipping — no lossless version on Qobuz."))
        return {"result": "lossy_only"}

    qobuz_tracks = (album.get("tracks") or {}).get("items") or []
    if not qobuz_tracks:
        log.info(fmt(C.RED, f"  ✗  {label_prefix}Album has no tracks in API response. Skipping."))
        return {"result": "no_tracks"}

    # ── Detect what's already there ──────────────────────────────────────────
    # --force cleanup is deferred until AFTER the download confirm
    # below: deleting here would mean a 'no' at the download prompt
    # leaves the user with their folder already wiped.
    force_cleaned = True
    if use_force:
        _, album_dir = find_existing_tracks(album)
        existing = []
        missing, present = qobuz_tracks, []
    else:
        existing, album_dir = find_existing_tracks(album)
        vlog(f"hybrid detection: {len(existing)} existing track(s) total")
        missing, present = compute_missing(qobuz_tracks, existing)

    # ── Quality-aware auto-upgrade decision ──────────────────────────────────
    # Runs BEFORE the "already complete" early-exit, because an album that
    # is "complete" track-wise might still be lower quality than Qobuz and
    # warrant an upgrade-replace. Disabled when --no-upgrade or --force is
    # in effect (--force has its own destructive flow with its own prompt).
    auto_upgrade_active = False    # if True, do backup-then-wipe-then-redownload
    upgrade_backup_path = None     # populated after backup_album_dir succeeds
    upgrade_existing_label = None  # before-quality label, set in the auto-upgrade branch

    # Quality-upgrade replace path. This is opt-in: it only runs when
    # AUTO_UPGRADE_ENABLED is set (env/Settings), or when the user invokes
    # the explicit Upgrade action (which turns the flag on for its run).
    # A plain gap-fill just fills the missing tracks and leaves the rest of
    # the album alone — it does NOT wipe-and-redownload an album to a
    # different master unless the user asked for upgrades.
    auto_upgrade = getattr(args, "auto_upgrade", cfg.AUTO_UPGRADE_ENABLED)
    if (auto_upgrade
            and existing
            and not use_force
            and not getattr(args, "no_upgrade", False)
            and album_dir is not None):
        qual = compare_album_quality(existing, album)
        cls = qual["classification"]
        extras = find_extras_in_existing(qobuz_tracks, existing)
        qbits, qrate = qual["qobuz_quality"]
        target_label = (f"{qbits}-bit/{qrate/1000:.1f}kHz"
                        if qbits and qrate else "Qobuz quality")

        if cls == "all_higher":
            # Existing strictly better than Qobuz everywhere — never replace.
            log.info(fmt(C.GREEN,
                f"\n  ✓  Already higher quality than Qobuz "
                f"({qual['n_above']} track(s) above target)."))
            if not missing:
                log_fetch({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "album_id": album.get("id"),
                    "artist": (album.get("artist") or {}).get("name"),
                    "title": album.get("title"),
                    "result": "skipped_already_higher_quality",
                    "tracks_total": len(qobuz_tracks),
                    "qobuz_quality": f"{qbits}-bit/{qrate}Hz",
                })
                return {"result": "skipped_already_higher_quality",
                        "n_total": len(qobuz_tracks)}
            # Has missing tracks. Will fall through to gap-fill prompt with
            # explicit warning that this creates mixed quality.
            log.info(fmt(C.YELLOW,
                f"     ⚠  {len(missing)} track(s) missing — filling them at "
                f"{target_label} would mix quality."))

        elif cls in ("all_lower", "mixed_below"):
            # Qobuz is higher quality, but only replace when it's verifiably
            # safe. An unreadable track (bit depth/rate we can't read) can't
            # be confirmed as an upgrade and a wipe-replace could silently
            # downgrade an unreadable hi-res file — refuse for the whole album
            # before any edition-swap path can reach the destructive branch,
            # and fill gaps instead. A gap-fill the user already okayed is
            # honoured below; auto-upgrade is the unsafe part, not gap-fill.
            if qual.get("n_unknown"):
                log.info(fmt(C.YELLOW,
                    f"\n  ⚠  Can't auto-upgrade — {qual['n_unknown']} track(s) "
                    f"have unreadable quality and would be replaced unverified; "
                    f"filling gaps only."))
                # Fall through to plain gap-fill; do NOT set auto_upgrade_active.
            elif extras and missing and already_confirmed:
                # User already okayed a gap-fill. We can't safely wipe-and-
                # replace while bonus tracks live on disk, but maybe an
                # expanded edition carries them — offer it. If it does and has
                # no extras of its own, upgrade; otherwise honor the gap-fill
                # rather than silently skipping.
                _exp, _exp_extras, _exp_qual = _offer_expanded_edition(
                    album, album_dir, existing, extras, token, args)
                if _exp is not None and _exp_qual["classification"] in ("all_lower", "mixed_below"):
                    log.info(fmt(C.MAGENTA,
                        f"\n  ↑  Switching to {_exp.get('title') or '?'!r} — "
                        f"covers your {len(existing)} tracks "
                        f"with {len(_exp_extras)} local-only at {album_quality_label(_exp)}"))
                    album = _exp
                    qobuz_tracks = (_exp.get("tracks") or {}).get("items") or []
                    extras = _exp_extras
                    qual = _exp_qual
                    qbits, qrate = _exp_qual["qobuz_quality"]
                    target_label = (f"{qbits}-bit/{qrate/1000:.1f}kHz"
                                    if qbits and qrate else "Qobuz quality")
                    missing, present = compute_missing(qobuz_tracks, existing)
                    if extras:
                        log.info(fmt(C.YELLOW,
                            f"\n  ⚠  Can't auto-upgrade ({len(extras)} bonus track(s) here); "
                            f"filling {len(missing)} at {target_label} (will mix quality)."))
                    else:
                        log.info(fmt(C.MAGENTA + C.BOLD,
                            f"\n  ↑ Auto-upgrade via expanded edition → {target_label}"))
                        auto_upgrade_active = True
                        existing = []
                        missing, present = qobuz_tracks, []
                else:
                    log.info(fmt(C.YELLOW,
                        f"\n  ⚠  Can't auto-upgrade ({len(extras)} bonus track(s) here); "
                        f"filling {len(missing)} at {target_label} (will mix quality)."))
            elif extras:
                # Before giving up, try to find an expanded edition that covers
                # the local tracks. Only switch when it also carries no extras
                # of its own — otherwise the wipe-replace would lose them.
                _exp, _exp_extras, _exp_qual = _offer_expanded_edition(
                    album, album_dir, existing, extras, token, args)
                if (_exp is not None
                        and _exp_qual["classification"] in ("all_lower", "mixed_below")
                        and not _exp_extras):
                    log.info(fmt(C.MAGENTA,
                        f"\n  ↑  Switching to {_exp.get('title') or '?'!r} — "
                        f"covers your {len(existing)} tracks at {album_quality_label(_exp)}"))
                    album = _exp
                    qobuz_tracks = (_exp.get("tracks") or {}).get("items") or []
                    extras = _exp_extras
                    qual = _exp_qual
                    qbits, qrate = _exp_qual["qobuz_quality"]
                    target_label = (f"{qbits}-bit/{qrate/1000:.1f}kHz"
                                    if qbits and qrate else "Qobuz quality")
                    missing, present = compute_missing(qobuz_tracks, existing)
                    log.info(fmt(C.MAGENTA + C.BOLD,
                        f"\n  ↑ Auto-upgrade via expanded edition → {target_label}"))
                    log.info(fmt(C.YELLOW,
                        "  ⚠  This was queued as a gap-fill but will now back up and\n"
                        "     replace the entire folder. Ctrl+C to abort."))
                    auto_upgrade_active = True
                    existing = []
                    missing, present = qobuz_tracks, []
                else:
                    log.info(fmt(C.YELLOW,
                        f"\n  ⚠  Upgrade to {target_label} blocked: "
                        f"{len(extras)} on-disk track(s) Qobuz doesn't carry:"))
                    for _e in extras[:5]:
                        log.info(fmt(C.GRAY,
                            f"       • {truncate(_e.get('title') or '?', 60)}"))
                    if len(extras) > 5:
                        log.info(fmt(C.GRAY,
                            f"       ... and {len(extras) - 5} more"))
                    log.info(fmt(C.GRAY,
                        "     If these look like normal album tracks, your tags"
                        " differ from Qobuz's. Skipping (logged)."))
                    log_fetch({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "album_id": album.get("id"),
                        "artist": (album.get("artist") or {}).get("name"),
                        "title": album.get("title"),
                        "result": "skipped_has_extras",
                        "tracks_total": len(qobuz_tracks),
                        "qobuz_quality": f"{qbits}-bit/{qrate}Hz",
                        "n_extras": len(extras),
                        "extra_titles": [t.get("title") or "?" for t in extras[:20]],
                    })
                    return {"result": "skipped_has_extras",
                            "n_total": len(qobuz_tracks),
                            "n_extras": len(extras)}
            else:
                # No extras — auto-upgrade is safe. Build a banner with an
                # explicit before→after quality contrast so the user sees at a
                # glance what they're getting.
                _qcounts = {}
                for _t in existing:
                    _bb, _rr = existing_track_quality(_t)
                    if _bb and _rr:
                        _qcounts[(_bb, _rr)] = _qcounts.get((_bb, _rr), 0) + 1
                if _qcounts:
                    _eb, _er = max(_qcounts, key=_qcounts.get)
                    existing_label = f"{_eb}-bit/{_er/1000:.1f}kHz"
                    if len(_qcounts) > 1:
                        existing_label = "mostly " + existing_label
                else:
                    existing_label = "lower-quality"
                upgrade_existing_label = existing_label
                _fill = f" +{len(missing)} gap-fill" if missing else ""
                if not already_confirmed:
                    log.info(fmt(C.MAGENTA + C.BOLD,
                        f"\n  ↑ Auto-upgrade: {existing_label} → {target_label}{_fill}"))
                auto_upgrade_active = True
                # Override detection results so downstream code does a full
                # download. Backup happens after user confirms.
                # upgrade_only: re-download ONLY the tracks already on disk
                # (skipping the missing ones). Album stays as incomplete as
                # it was, just at the higher quality.
                if upgrade_only:
                    # map each local track to its single
                    # best Qobuz match so duplicate matches (one local
                    # file matching both "Foo" and "Foo (Edit)" via
                    # title-stripping) don't cause re-ripping the same
                    # track twice — which would collide at the destination
                    # filename and produce a 0-byte fallback that
                    # cleanup_lossy then deletes, losing the original.
                    _claimed_qids = set()
                    _upgrade_targets = []
                    for _et in existing:
                        _eisrc = _et.get("isrc") or ""
                        _embid = _et.get("mb_trackid") or ""
                        _enorm = _et.get("normalized") or ""
                        _edisc = _et.get("discnumber", 1) or 1
                        _estripped = normalize(strip_edition_suffix(
                            _et.get("title") or ""))
                        _best_qt, _best_rank = None, 99
                        for _qt in present:
                            if _qt.get("id") in _claimed_qids:
                                continue
                            _qisrc = (_qt.get("isrc") or "").replace("-", "").upper()
                            _qmbid = (_qt.get("mbid") or "").lower()
                            _qnorm = normalize(_qt.get("title") or "")
                            _qstripped = normalize(strip_edition_suffix(
                                _qt.get("title") or ""))
                            _qdisc = _qt.get("media_number", 1) or 1
                            if _eisrc and _qisrc and _eisrc == _qisrc:
                                _r = 0
                            elif _embid and _qmbid and _embid == _qmbid:
                                _r = 1
                            elif _qdisc == _edisc and _enorm and _enorm == _qnorm:
                                _r = 2
                            elif _qdisc == _edisc and _estripped and _estripped == _qstripped:
                                _r = 3
                            else:
                                continue
                            if _r < _best_rank:
                                _best_qt, _best_rank = _qt, _r
                                if _r == 0:
                                    break
                        if _best_qt is not None:
                            _claimed_qids.add(_best_qt.get("id"))
                            _upgrade_targets.append(_best_qt)
                    existing = []
                    missing, present = _upgrade_targets, []
                else:
                    existing = []
                    missing, present = qobuz_tracks, []

        elif cls == "mixed_above":
            # Some tracks above, rest equal. Treat like all_higher.
            log.info(fmt(C.GREEN,
                "\n  ✓  No upgrade available (some track(s) already above target)."))
            if not missing:
                log_fetch({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "album_id": album.get("id"),
                    "artist": (album.get("artist") or {}).get("name"),
                    "title": album.get("title"),
                    "result": "skipped_already_higher_quality",
                    "tracks_total": len(qobuz_tracks),
                })
                return {"result": "skipped_already_higher_quality",
                        "n_total": len(qobuz_tracks)}
            # Has missing tracks. Mirror the all_higher branch warning so
            # the user knows filling them at Qobuz quality creates a mixed
            # bag against tracks that are already above target.
            log.info(fmt(C.YELLOW,
                f"     ⚠  {len(missing)} track(s) missing — filling them at "
                f"{target_label} would mix quality."))

        elif cls == "mixed_both":
            # Some above, some below: ambiguous. Don't auto-replace.
            log.info(fmt(C.YELLOW,
                f"\n  ⚠  Mixed quality: {qual['n_above']} above and "
                f"{qual['n_below']} below Qobuz target. Not auto-upgrading."))
            log.info(fmt(C.GRAY,
                "     Falling back to gap-fill if applicable. "
                "Use --force on this album manually if you want to replace."))

        elif cls == "unknown":
            log.info(fmt(C.GRAY,
                "  · Couldn't read quality from existing tracks; using gap-fill."))

        # cls == "all_equal": no message, today's behavior.

    # If caller asked for upgrade-only but the auto-upgrade branch above
    # didn't fire (quality classification didn't match upgrade criteria,
    # extras blocked it, etc.), bail rather than silently fall through to
    # gap-fill — the user explicitly asked NOT to fill missing tracks.
    if upgrade_only and not auto_upgrade_active:
        log.info(fmt(C.YELLOW,
            "  ⚠  Upgrade-only requested but no upgrade applies here; skipping."))
        return {"result": "upgrade_only_no_op", "n_total": len(qobuz_tracks)}

    if not already_confirmed:
        print_album_summary(album, missing, present, album_dir, use_force,
                            auto_upgrade=auto_upgrade_active,
                            existing_quality_label=upgrade_existing_label)

    if not missing:
        log.info(fmt(C.GREEN, "\n  ✓  Already complete. Nothing to download.\n"))
        log_fetch({
            "ts": datetime.now(timezone.utc).isoformat(),
            "album_id": album.get("id"),
            "artist": (album.get("artist") or {}).get("name"),
            "title": album.get("title"),
            "result": "already_complete",
            "tracks_total": len(qobuz_tracks),
            "tracks_downloaded": 0,
        })
        if args.consolidate:
            try:
                consolidate_albums(album, args)
            except KeyboardInterrupt:
                log.info(fmt(C.GRAY, "\n  Consolidation interrupted."))
        return {"result": "already_complete", "n_total": len(qobuz_tracks)}

    # A dry run has already printed the plan above; stop before the download
    # confirm so we don't ask "proceed with downloading?" for a run that never
    # downloads.
    if args.dry_run:
        log.info(fmt(C.YELLOW, "\n  --dry-run: stopping here, nothing downloaded.\n"))
        return {"result": "dry_run", "n_missing": len(missing)}

    # Default NO: the missing-tracks list above is the user's chance to
    # decide; defaulting yes would mean every enter-press starts a download
    # they may not want. Press y to proceed.
    if not already_confirmed and not confirm(
            f"\n  Proceed with downloading {len(missing)} track(s)?",
            default_yes=False, auto_yes=args.yes):
        log.info(fmt(C.GRAY, "  Skipped."))
        return {"result": "user_skipped", "n_missing": len(missing)}

    # ── Pre-flight: staging dir state (BEFORE backup so sys.exit can't strand it)
    staging_preflight(args)

    # ── Auto-upgrade: back up the existing folder before redownload ──────────
    # Same-filesystem move, so this is fast (rename, not copy). The backup
    # is restored if anything fails before beets import succeeds.
    if auto_upgrade_active and album_dir and album_dir.exists():
        upgrade_backup_path = backup_album_dir(album_dir)
        if upgrade_backup_path is None:
            log.info(fmt(C.RED,
                "  ✗  Could not back up the existing folder; refusing to "
                "wipe without a backup. Skipping this album."))
            log_fetch({
                "ts": datetime.now(timezone.utc).isoformat(),
                "album_id": album.get("id"),
                "artist": (album.get("artist") or {}).get("name"),
                "title": album.get("title"),
                "result": "upgrade_aborted_backup_failed",
            })
            return {"result": "upgrade_aborted_backup_failed"}
        log.info(fmt(C.GRAY,
            "  ⤷  Backed up existing folder (auto-restore on failure)"))

    # ── --force: NOW move the existing album dir aside (deferred from above) ──
    if use_force:
        force_outcome = force_cleanup_preflight(album, args)
        if force_outcome is None:
            # Symlink — no safe --force path; skip without asking anything further.
            return {"result": "cancelled"}
        if force_outcome is False:
            if not confirm("\n  Proceed with --force despite expected '<n>.1.flac' collisions?",
                           default_yes=False, auto_yes=False):
                log.info(fmt(C.GRAY, "  Skipping this album."))
                return {"result": "cancelled"}
        elif isinstance(force_outcome, Path) and upgrade_backup_path is None:
            # The moved-aside folder is restored on failure / cleared on success
            # by the same finally block that handles the auto-upgrade backup.
            upgrade_backup_path = force_outcome

    # ── Download phase ───────────────────────────────────────────────────────
    # Pre-init so the backup-resolution finally block has sane defaults if a
    # rip raises AuthLost / OSError before the result is read back. The
    # download itself lives in run_album_download, shared with the queue
    # executor; it writes its outcome into download_result as it goes (the
    # gap-fill backup path the moment it's taken) so the finally can still
    # restore it on a raise.
    n_ok = n_fail = n_lossy = 0
    failed_tracks, lossy_tracks, broken_tracks = [], [], []
    imported = False
    elapsed = 0.0
    download_phase_completed = False
    transient_lyric_sigs = []
    download_result = {}

    try:
        snapshot = snapshot_staging()
        vlog(f"staging snapshot: {len(snapshot)} files before download")

        run_album_download(
            album=album, missing=missing, present=present, existing=existing,
            album_dir=album_dir, snapshot=snapshot, quality=quality,
            upgrade_only=upgrade_only, result=download_result)
        n_ok = download_result["n_ok"]
        n_fail = download_result["n_fail"]
        n_lossy = download_result["n_lossy"]
        failed_tracks = download_result["failed_tracks"]
        lossy_tracks = download_result["lossy_tracks"]
        broken_tracks = download_result.get("broken_tracks", [])
        elapsed = download_result["elapsed"]

        # Cancelled mid-rip: the user hit stop, so the partial set must not be
        # imported. Discard just this job's staged files and bail before the
        # pre-import hooks + beets ever run.
        if is_cancel_requested():
            log.info(fmt(C.YELLOW,
                "\n  Cancelled — discarding the partial download; nothing imported."))
            _discard_staged_since(snapshot)
            return {"result": "cancelled", "imported": False,
                    "n_ok": n_ok, "n_lossy": n_lossy, "n_fail": n_fail}

        # ── Pre-import: compress + lyrics on STAGING ─────────────────────────────
        # compress + lyric_fetch run on staging BEFORE beets imports.
        # Beets's `move: yes` then transfers already-compressed,
        # already-lyriced FLACs into the library in one shot, so a media
        # server only ever sees the final state and never serves stale
        # metadata (wrong sample rate, missing lyrics) to its clients
        # between the move and the post-import hooks.
        if n_ok > 0 and not args.no_import:
            transient_lyric_sigs = _pre_import_staging_hooks(args)

        # ── Beets import ─────────────────────────────────────────────────────────
        imported = False
        if args.no_import:
            log.info(fmt(C.YELLOW, f"\n  --no-import: skipping beets. Files remain in {cfg.STAGING_DIR}/"))
        elif n_ok == 0:
            log.info(fmt(C.YELLOW, "\n  Skipping beets import — nothing succeeded."))
        else:
            log.info("")
            # A brand-new album (no folder found on disk) lands in a fresh
            # directory, so it can't split an existing beets album into
            # duplicate rows — skip the full-library de-dup scan for it.
            imported = beets_import_paths(consolidate=album_dir is not None)

        download_phase_completed = True

    finally:
        # Always resolve upgrade backup, including on exception.
        # download_phase_completed stays False on KeyboardInterrupt /
        # AuthLost / SystemExit / unhandled errors → restore branch fires.
        # ── Auto-upgrade backup resolution ───────────────────────────────────────
        upgrade_restored = False
        if upgrade_backup_path is not None:
            # Require zero failures AND zero lossy-deletes.
            upgrade_succeeded = (download_phase_completed
                                  and imported
                                  and n_ok > 0
                                  and n_fail == 0
                                  and n_lossy == 0)
            # An auto-upgrade wipes the only full copy, so it has to clear a
            # higher bar before the backup is deleted: the rebuilt folder must
            # be verifiably at least as complete as the original (same-or-more
            # tracks and playtime). The decode gate above proves each file
            # plays, not that the matcher kept every track or that a re-rip
            # isn't short. A --force replace is a deliberate override (it may
            # swap a different/smaller edition on purpose), so it skips this.
            upgrade_verified = upgrade_succeeded and (
                not auto_upgrade_active
                or _upgrade_replacement_verified(
                    album, album_dir, upgrade_backup_path))
            if upgrade_verified:
                try:
                    shutil.rmtree(upgrade_backup_path)
                    if auto_upgrade_active and not upgrade_only:
                        log.info(fmt(C.GRAY,
                            "  ✓  Upgrade complete; backup cleared."))
                except OSError as e:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Upgrade succeeded but couldn't remove backup: {e}."))
                    log.info(fmt(C.GRAY,
                        f"     Backup remains at {upgrade_backup_path} "
                        f"(auto-cleaned after {cfg.UPGRADE_BACKUP_RETENTION_DAYS} days)."))
            elif upgrade_succeeded and auto_upgrade_active:
                # Passed the decode/lossy gate but the rebuilt folder isn't
                # verifiably as complete as the original — keep the only full
                # copy instead of deleting it.
                log.info(fmt(C.YELLOW,
                    "\n  ⚠  Upgrade couldn't be verified as complete; "
                    "keeping your original."))
                log.info(fmt(C.GRAY,
                    f"     Original preserved at {upgrade_backup_path} "
                    f"(auto-cleaned after {cfg.UPGRADE_BACKUP_RETENTION_DAYS} days)."))
                log.info(fmt(C.WHITE,
                    f"     Restore: mv {upgrade_backup_path!s} {album_dir!s}"))
            elif download_phase_completed and args.no_import:
                log.info(fmt(C.YELLOW,
                    f"\n  ⚠  --no-import set; cannot auto-verify upgrade. "
                    f"Backup kept at {upgrade_backup_path}."))
            else:
                log.info(fmt(C.YELLOW,
                    "\n  ⚠  Upgrade did not succeed (no successful import); "
                    "restoring backup …"))
                upgrade_restored = restore_upgrade_backup(upgrade_backup_path, album_dir)
                if upgrade_restored:
                    log.info(fmt(C.GREEN,
                        f"  ✓  Restored original folder to {album_dir}."))
                else:
                    log.info(fmt(C.RED,
                        f"  ✗  Auto-restore failed. Original folder is at: "
                        f"{upgrade_backup_path}"))
                    log.info(fmt(C.WHITE,
                        f"     Manual restore: mv {upgrade_backup_path!s} {album_dir!s}"))

        # ── Gap-fill backup resolution ───────────────────────────────────────
        # run_album_download records this the moment it stashes present tracks,
        # so it's reachable here even when the rip raised before returning.
        gap_fill_backup_path = download_result.get("gap_fill_backup_path")
        if gap_fill_backup_path is not None and gap_fill_backup_path.exists():
            gap_fill_succeeded = (download_phase_completed
                                  and imported
                                  and n_ok > 0
                                  and n_fail == 0
                                  and n_lossy == 0)
            if gap_fill_succeeded:
                try:
                    shutil.rmtree(gap_fill_backup_path)
                except OSError as e:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Gap-fill complete but couldn't remove backup: {e}."))
            elif args.no_import:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  --no-import; gap-fill backup kept at "
                    f"{gap_fill_backup_path}."))
            else:
                log.info(fmt(C.YELLOW,
                    "  ⚠  Gap-fill did not succeed; restoring backed-up tracks…"))
                _n_back = restore_gap_fill_backup(gap_fill_backup_path, album_dir)
                log.info(fmt(C.GREEN,
                    f"  ✓  Restored {_n_back} track(s) to {album_dir}."))

        # `beet import` only moves audio. Streamrip's __artwork/ cover-image
        # dirs are left behind in staging; sweep them once the import path
        # has had its turn. Skipped when --no-import (the user wants staging
        # left intact for manual review).
        if imported and not args.no_import:
            sweep_staging_artwork()

    # ── Consolidation ────────────────────────────────────────────────────────
    n_consolidated = 0
    if args.consolidate:
        if imported:
            try:
                n_consolidated = consolidate_albums(album, args)
            except KeyboardInterrupt:
                log.info(fmt(C.GRAY, "\n  Consolidation interrupted."))
        else:
            log.info(fmt(C.YELLOW,
                "\n  --consolidate requested but beets import didn't succeed — skipping."))

    # ── Post-import cleanup: duplicate cover art ────────────────────────────
    if imported:
        # Multi-artist folder migration is opt-in only (--migrate-multi-artist).
        # Only migrate on strict success (no fails, no lossy).
        _strict_success = (n_fail == 0 and n_lossy == 0)
        if (getattr(args, "migrate_multi_artist", False)
                and _strict_success):
            post_dir = prompt_and_migrate_multi_artist_folder(album, args)
            if post_dir is None:
                post_dir = find_album_dir_filesystem(album)
        else:
            post_dir = find_album_dir_filesystem(album)
        # A brand-new album can land in a folder the cached listing predates;
        # clear the cache and look once more before giving up, or art cleanup,
        # the split-merge, and the lyric-retry queue all silently no-op.
        if post_dir is None:
            clear_scan_caches()
            post_dir = find_album_dir_filesystem(album)
        if post_dir:
            n_art_removed = cleanup_duplicate_art(post_dir)
            if n_art_removed:
                vlog(f"removed {n_art_removed} duplicate art file(s)")
            # Split-folder auto-merge.
            try:
                split_artist = (album.get("artist") or {}).get("name") or ""
                if _is_split_album_merge(album_dir, post_dir, split_artist):
                    n_merged = _merge_split_folder(post_dir, album_dir)
                    if n_merged:
                        log.info(fmt(C.GREEN,
                            f"  ✓  Consolidated {n_merged} existing track(s) "
                            f"into primary-artist folder: {post_dir.name}."))
                    else:
                        log.info(fmt(C.YELLOW,
                            f"  ⚠  Split-folder detected but nothing merged "
                            f"(check {album_dir} for overlap conflicts)."))
            except Exception as _e_sf:
                vlog(f"split-folder merge raised: {_e_sf}")
            # Resolve transient-lyric signatures captured pre-beets
            if transient_lyric_sigs:
                resolved = _resolve_signatures_to_paths(
                    transient_lyric_sigs, [post_dir])
                if resolved:
                    _record_post_import_lyric_retry(resolved)
                    vlog(f"lyric retry: queued {len(resolved)} "
                         f"post-import path(s) for next-launch retry")
            # Materialise .lrc sidecars next to the final renamed files
            # (no-op unless LYRICS_FORMAT is sidecar/both).
            try:
                write_post_import_sidecars([post_dir, album_dir])
            except Exception as _e_sc:
                vlog(f"post-import sidecar write raised: {_e_sc}")
    elif transient_lyric_sigs:
        # Import didn't succeed (beets failed, silent skip, or n_ok==0):
        # the files are still in STAGING_DIR. Record their staging paths
        # so next launch's offer_resume_lyric_retry gets a second chance
        # at provider-unavailable lyrics instead of silently dropping the
        # queue. Stale entries (files beets later moves on a retry walk)
        # self-prune in offer_resume_lyric_retry.
        resolved = _resolve_signatures_to_paths(
            transient_lyric_sigs, [cfg.STAGING_DIR])
        if resolved:
            _record_post_import_lyric_retry(resolved)
            vlog(f"lyric retry: import unsuccessful; queued {len(resolved)} "
                 f"staging path(s) for next-launch retry")

    # ── Summary ──────────────────────────────────────────────────────────────
    if already_confirmed and not n_fail and not n_lossy and imported:
        if auto_upgrade_active:
            log.info(fmt(C.MAGENTA + C.BOLD,
                f"  ↑ upgraded · {n_ok} track(s) · {int(elapsed)}s · imported"))
        else:
            log.info(fmt(C.GREEN,
                f"  ✓ {n_ok} downloaded · {int(elapsed)}s · imported"))
    else:
        section("Result")
        log.info("")
        log.info(f"  {fmt(C.GREEN if n_ok else C.GRAY,    '✓ downloaded:')}    {n_ok}")
        n_truly_lossy = n_lossy - len(broken_tracks)
        if n_truly_lossy:
            log.info(f"  {fmt(C.YELLOW, '⚠ lossy on Qobuz:')} {n_truly_lossy}")
        if broken_tracks:
            log.info(f"  {fmt(C.YELLOW, '⚠ incomplete:')}    {len(broken_tracks)}")
        if n_fail:
            log.info(f"  {fmt(C.RED, '✗ failed:')}        {n_fail}")
        log.info(f"  {fmt(C.GRAY, '  runtime:')}        {int(elapsed)}s")
        log.info(f"  {fmt(C.GRAY, '  beets:')}          {'imported' if imported else 'skipped/failed'}")
        if args.consolidate:
            log.info(f"  {fmt(C.GRAY, '  consolidated:')}   {plural(n_consolidated, 'sibling track')} removed")
        if failed_tracks:
            log.info(fmt(C.RED, "\n  failed tracks:"))
            for t in failed_tracks[:10]:
                log.info(f"     {truncate(t, 60)}")
        _broken = set(broken_tracks)
        truly_lossy = [t for t in lossy_tracks if t not in _broken]
        if truly_lossy:
            log.info(fmt(C.YELLOW,
                "\n  only available lossy on Qobuz (would need another source):"))
            for t in truly_lossy[:10]:
                log.info(f"     {truncate(t, 60)}")
        if broken_tracks:
            log.info(fmt(C.YELLOW,
                "\n  downloaded incomplete and discarded (a re-run usually fixes these):"))
            for t in broken_tracks[:10]:
                log.info(f"     {truncate(t, 60)}")
        log.info("")

    if n_ok and n_fail:
        result_status = "partial"
    elif n_ok:
        result_status = "downloaded"
    elif n_fail:
        result_status = "failed"
    else:
        result_status = "nothing_landed"

    log_fetch({
        "ts": datetime.now(timezone.utc).isoformat(),
        "album_id": album.get("id"),
        "artist": (album.get("artist") or {}).get("name"),
        "title": album.get("title"),
        "result": result_status,
        "tracks_total": len(qobuz_tracks),
        "tracks_already_present": len(present),
        "tracks_attempted": len(missing),
        "tracks_downloaded": n_ok,
        "tracks_lossy_deleted": n_lossy,
        "tracks_failed": n_fail,
        "failed_titles": failed_tracks,
        "lossy_titles": lossy_tracks,
        "broken_titles": broken_tracks,
        "imported": imported,
        "force": bool(use_force),
        "force_cleaned": force_cleaned,
        "auto_upgrade": bool(auto_upgrade_active),
        "upgrade_backup_path": str(upgrade_backup_path) if upgrade_backup_path else None,
        "upgrade_restored": upgrade_restored,
        "consolidated": bool(args.consolidate and imported),
        "consolidated_tracks_removed": n_consolidated,
        "elapsed_s": int(elapsed),
    })

    return {
        "result": result_status,
        "n_ok": n_ok, "n_fail": n_fail, "n_lossy": n_lossy,
        "imported": imported,
        "auto_upgrade": bool(auto_upgrade_active),
    }
