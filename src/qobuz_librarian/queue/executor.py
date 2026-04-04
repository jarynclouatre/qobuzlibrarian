"""Queue executor — download, pre-import hooks, beets, backup resolution.

"""
import re
import shutil
import time
from datetime import datetime, timezone

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost
from qobuz_librarian.integrations.beets import (
    _consolidate_duplicate_albums,
    beets_import_albums,
    staging_preflight,
)
from qobuz_librarian.integrations.compress import HAVE_DOWNSAMPLE, downsample_dir
from qobuz_librarian.integrations.lyrics import (
    _record_post_import_lyric_retry,
    _resolve_signatures_to_paths,
    _run_lyric_hook,
    write_post_import_sidecars,
)
from qobuz_librarian.integrations.rip import (
    cleanup_lossy,
    files_added_since,
    is_cancel_requested,
    rip_url,
    snapshot_staging,
)
from qobuz_librarian.library.backup import (
    backup_album_dir,
    backup_gap_fill_files,
    restore_gap_fill_backup,
    restore_upgrade_backup,
)
from qobuz_librarian.library.catalog import (
    _count_audio_files_in,
    _is_split_album_merge,
    cleanup_duplicate_art,
    find_album_dir_filesystem,
    find_extras_in_existing,
    prompt_and_migrate_multi_artist_folder,
)
from qobuz_librarian.library.scanner import clear_scan_caches, read_album_dir
from qobuz_librarian.library.tags import normalize, strip_edition_suffix
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import log_fetch


def _track_title_from_path(p):
    """Strip track-number prefix and artist prefix from a path stem to get the bare title."""
    s = p.stem if hasattr(p, "stem") else str(p)
    m = re.match(r"^(?:\d+[-.])?\d+[\s\-–—.]+(.+)$", s)
    title = m.group(1) if m else s
    m = re.match(r"^.+?\s+-\s+(.+)$", title)
    return m.group(1) if m else title


def _download_for_queue_item(item):
    """Download phase for one queue item: rip + cleanup_lossy + reconcile.
    Mutates item with n_ok/n_fail/etc. AuthLost & KeyboardInterrupt
    propagate to caller."""
    from qobuz_librarian.api.auth import detect_auth_lost, detect_disk_full, detect_rate_limited
    rate_limited = False

    album = item["album"]
    missing = item["missing"]
    present = item["present"]
    upgrade_only = item["upgrade_only"]
    qobuz_tracks = (album.get("tracks") or {}).get("items") or []
    n_tracks_total = len(qobuz_tracks)

    # Same upgrade_only fix as process_album above.
    if item.get("force_track_by_track"):
        # Repair sets this: download exactly the truncated tracks, never
        # the whole-album URL. Decoupled from the ratio heuristic on
        # purpose so changing that heuristic can't turn a targeted repair
        # into a wipe-and-replace.
        download_full_album = False
    elif upgrade_only:
        download_full_album = (len(missing) == n_tracks_total)
    else:
        download_full_album = (
            len(present) == 0
            or len(missing) >= max(4, int(n_tracks_total * 0.7))
        )
    album_id = album.get("id")
    t_start = time.time()
    n_fail = 0
    failed_tracks = []
    full_album_rc = None

    # Surface the chosen path so a wipe-and-fill is distinguishable
    # from a targeted gap-fill in the live log.
    if download_full_album:
        log.info(fmt(C.GRAY,
            f"  Strategy: full-album URL "
            f"({len(missing)} of {n_tracks_total} missing)"))
    else:
        _why = "forced per-track (repair)" if item.get("force_track_by_track") \
            else f"{len(missing)} of {n_tracks_total} missing"
        log.info(fmt(C.GRAY, f"  Strategy: per-track ({_why})"))

    if download_full_album:
        url = f"https://play.qobuz.com/album/{album_id}"
        # Remove already-present tracks before ripping so beets doesn't
        # create 'Foo.1.flac' duplicates during import. Move them to a
        # backup dir first; the backup-resolution pass at the end of the
        # batch will restore them if the queue item ends up failing (rip
        # error, beets crash, KeyboardInterrupt during compress hook).
        _qi_album_dir = item.get("album_dir")
        _qi_present = item.get("present") or []
        if _qi_present and _qi_album_dir:
            _qi_existing = read_album_dir(_qi_album_dir)
            _qi_extras = find_extras_in_existing(qobuz_tracks, _qi_existing)
            _qi_extra_paths = {e["path"] for e in _qi_extras}
            _qi_to_clear = [e for e in _qi_existing
                            if e["path"] not in _qi_extra_paths]
            if _qi_to_clear:
                vlog(f"pre-download: backing up + removing {len(_qi_to_clear)} present "
                     f"track(s) to prevent .1.flac collisions")
                item["gap_fill_backup_path"] = backup_gap_fill_files(
                    [e["path"] for e in _qi_to_clear], _qi_album_dir)
        rc, out = rip_url(url, timeout=cfg.RIP_TIMEOUT, live_output=True,
                          quality=item.get("quality"))
        full_album_rc = rc
        if detect_auth_lost(out):
            raise AuthLost("rip output contained auth-lost markers")
        if detect_disk_full(out):
            raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
        rate_limited = rate_limited or detect_rate_limited(out)
        n_errors = len(re.findall(
            r"^\s*(?:\[\d{2}:\d{2}:\d{2}\]\s*)?ERROR\b",
            out, re.MULTILINE))
        if rc != 0:
            log.info(fmt(C.RED, f"    ✗  rip exit {rc}"))
        elif n_errors:
            log.info(fmt(C.YELLOW,
                f"    ⚠  {n_errors} error(s) in rip output"))
    else:
        for i, t in enumerate(missing, 1):
            if is_cancel_requested():
                break
            tid = t.get("id")
            ttl = t.get("title") or "?"
            log.info(fmt(C.BLUE, f"      [{i}/{len(missing)}]") +
                     f" {fmt(C.WHITE, truncate(ttl, 56))}")
            url = f"https://play.qobuz.com/track/{tid}"
            rc, out = rip_url(url, timeout=cfg.RIP_TIMEOUT,
                              quality=item.get("quality"))
            if detect_auth_lost(out):
                raise AuthLost("rip output contained auth-lost markers")
            if detect_disk_full(out):
                raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
            rate_limited = rate_limited or detect_rate_limited(out)
            if rc == 0:
                log.info(fmt(C.GREEN, "        ✓ ok"))
            elif is_cancel_requested():
                # rip exited because we asked it to stop — not a failure.
                break
            else:
                n_fail += 1
                failed_tracks.append(ttl)
                log.info(fmt(C.RED, f"        ✗ rip exit {rc}"))
            time.sleep(cfg.DELAY_BETWEEN)

    new_files = files_added_since(item["snapshot_before"])
    audio_new = [f for f in new_files if f.suffix.lower() in cfg.AUDIO_EXTS]
    kept, deleted = cleanup_lossy(audio_new)
    n_ok = len(kept)
    n_lossy = len(deleted)
    lossy_tracks = deleted

    # Single-track retry for lossy / 0-byte fallbacks, mirroring the direct
    # album path (modes/process.py). A transient glitch shouldn't strand a
    # track in the lossy bucket when a one-off per-track URL usually
    # succeeds. One retry per track — no recursion, no loop.
    if lossy_tracks and missing:
        lossy_norms = {normalize(strip_edition_suffix(_track_title_from_path(d)))
                       for d in lossy_tracks}
        retry_targets = [
            t for t in missing
            if normalize(strip_edition_suffix(t.get("title") or "")) in lossy_norms
        ]
        if retry_targets:
            log.info(fmt(C.GRAY,
                f"      ↻  Retrying {len(retry_targets)} lossy/empty "
                "track(s) once via per-track URL"))
            retry_snapshot = snapshot_staging()
            for t in retry_targets:
                tid = t.get("id")
                if not tid:
                    continue
                rc, out = rip_url(f"https://play.qobuz.com/track/{tid}",
                                  timeout=cfg.RIP_TIMEOUT,
                                  quality=item.get("quality"))
                if detect_auth_lost(out):
                    raise AuthLost("rip output contained auth-lost markers")
                if detect_disk_full(out):
                    raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
                rate_limited = rate_limited or detect_rate_limited(out)
            retry_audio = [f for f in files_added_since(retry_snapshot)
                           if f.suffix.lower() in cfg.AUDIO_EXTS]
            retry_kept, _ = cleanup_lossy(retry_audio)
            if retry_kept:
                recovered_norms = {
                    normalize(strip_edition_suffix(_track_title_from_path(p)))
                    for p in retry_kept
                }
                lossy_tracks = [
                    d for d in lossy_tracks
                    if normalize(strip_edition_suffix(_track_title_from_path(d)))
                    not in recovered_norms
                ]
                kept = kept + retry_kept
                n_ok = len(kept)
                n_lossy = len(lossy_tracks)
                log.info(fmt(C.GREEN,
                    f"      ✓  Retry recovered {len(retry_kept)} track(s)"))

    if download_full_album and full_album_rc is not None:
        # Lossy-fallback tracks aren't a failure; subtract them so the
        # math holds (``n_ok + n_lossy + n_fail == n_attempted``).
        n_fail = max(0, len(missing) - n_ok - n_lossy)
        if n_fail > 0:
            surviving = {normalize(strip_edition_suffix(_track_title_from_path(p))) for p in kept}
            lossy_norms = {
                normalize(strip_edition_suffix(_track_title_from_path(stem)))
                for stem in lossy_tracks
            }
            failed_tracks = [
                t.get("title") for t in missing
                if normalize(strip_edition_suffix(t.get("title", ""))) not in surviving
                and normalize(strip_edition_suffix(t.get("title", ""))) not in lossy_norms
            ]
        else:
            failed_tracks = []
        # Surface reconciliation. If rip exited non-zero but every
        # track landed, say so; users were left guessing whether the
        # green summary line was real.
        if full_album_rc != 0 and n_fail == 0 and n_ok > 0:
            log.info(fmt(C.GRAY,
                f"      · {n_ok} track(s) landed despite rip exit "
                f"{full_album_rc} (streamrip post-processing error)"))
    elif failed_tracks and kept:
        surviving = {normalize(strip_edition_suffix(_track_title_from_path(p))) for p in kept}
        recovered = [t for t in failed_tracks
                     if normalize(strip_edition_suffix(t)) in surviving]
        if recovered:
            failed_tracks = [t for t in failed_tracks
                             if normalize(strip_edition_suffix(t)) not in surviving]
            n_fail = max(0, n_fail - len(recovered))
            # Surface per-track reconciliation.
            log.info(fmt(C.GRAY,
                f"      · {len(recovered)} track(s) reported failure "
                f"but landed on disk — counting as success"))

    item["n_ok"] = n_ok
    item["n_fail"] = n_fail
    item["n_lossy"] = n_lossy
    item["failed_tracks"] = failed_tracks
    item["lossy_tracks"] = lossy_tracks
    item["rate_limited"] = rate_limited
    item["elapsed"] = time.time() - t_start


_DISC_FOLDER_RE = re.compile(r"^(?:disc|cd)\s*\d+", re.IGNORECASE)


def _staged_album_dirs(item):
    """Top-level album dirs (under STAGING_DIR) that hold this item's freshly
    downloaded audio. Disc-folder children roll up to the album dir so beets
    sees a multi-disc release as one album, not one album per disc."""
    snap = item.get("snapshot_before") or set()
    audio = [p for p in files_added_since(snap)
             if p.suffix.lower() in cfg.AUDIO_EXTS]
    if not audio:
        return []
    album_dirs = set()
    for p in audio:
        parent = p.parent
        if _DISC_FOLDER_RE.match(parent.name):
            parent = parent.parent
        album_dirs.add(parent)
    return sorted(album_dirs)


def _run_pre_import_hooks_for_dirs(album_dirs, args):
    """Run compress + lyric hooks scoped to ``album_dirs``. Returns the
    aggregated lyric-signature list so the post-import resolver can map them
    to library paths once beets has moved the files. Raises KeyboardInterrupt
    after logging if either hook is interrupted."""
    sigs = []
    if (cfg.DOWNSAMPLE_HIRES_ENABLED and HAVE_DOWNSAMPLE
            and not getattr(args, "no_downsample",
                            getattr(args, "no_compress", False))):
        for d in album_dirs:
            try:
                downsample_dir(d, verbose=True, base_dir=d, log=log.info)
            except KeyboardInterrupt:
                log.info(fmt(C.YELLOW, "  ⚠  downsample hook interrupted"))
                raise
            except Exception as _ce:
                log.info(fmt(C.YELLOW, f"  ⚠  downsample hook failed: {_ce}"))
    for d in album_dirs:
        try:
            lh_result = _run_lyric_hook(d)
        except KeyboardInterrupt:
            log.info(fmt(C.YELLOW, "  ⚠  lyric hook interrupted"))
            raise
        except Exception as _le:
            log.info(fmt(C.YELLOW, f"  ⚠  lyric hook failed: {_le}"))
            lh_result = None
            # Hook crashed; capture signatures of every staged FLAC under this
            # album dir so the post-import resolution still has a shot. Recording
            # staging paths here would go stale once beets moves the files.
            try:
                from qobuz_librarian.integrations.rip import _flac_signature
                for p in sorted(d.rglob("*.flac")):
                    sig = _flac_signature(p)
                    if sig is not None:
                        sigs.append((sig, str(p)))
            except Exception:
                pass
        if isinstance(lh_result, tuple) and len(lh_result) == 2:
            sigs.extend(lh_result[1])
    return sigs


def _pre_import_staging_hooks(args):
    """Whole-staging pre-import hooks (back-compat).

    Used by the single-album process path (``modes/process.py``) where
    staging holds exactly one album at hook time, so scoping to the whole
    staging dir is equivalent to per-album. The queue executor calls
    ``_run_pre_import_hooks_for_dirs`` directly with the per-item album dirs.
    """
    return _run_pre_import_hooks_for_dirs([cfg.STAGING_DIR], args)


def _import_album_with_retry(album_dirs):
    """Run beets import on ``album_dirs`` with up to ``BEETS_MAX_ATTEMPTS``
    attempts, retrying only on idle-timeout (other failures aren't transient).
    Returns True on import success, False on permanent failure."""
    if not album_dirs:
        return True
    max_attempts = max(1, int(cfg.BEETS_MAX_ATTEMPTS))
    for attempt in range(1, max_attempts + 1):
        kind = beets_import_albums(album_dirs)
        if kind == "ok":
            return True
        if kind == "timeout" and attempt < max_attempts:
            log.info(fmt(C.YELLOW,
                f"  ⏳ beets import timed out (attempt {attempt}/{max_attempts}); "
                f"pausing {int(cfg.BEETS_RETRY_PAUSE)}s before retry."))
            time.sleep(cfg.BEETS_RETRY_PAUSE)
            continue
        return False
    return False


def _move_to_beets_retry(album_dirs, label):
    """Move album dirs that failed all beets attempts into
    STAGING_DIR/<BEETS_RETRY_DIR>/<timestamp>-<label>/ so the next batch
    isn't dragged down by them. The user can replay later with
    ``beet import`` of that subtree."""
    if not album_dirs:
        return
    retry_root = cfg.STAGING_DIR / cfg.BEETS_RETRY_DIR
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:60] or "album"
    dest_parent = retry_root / f"{stamp}-{safe_label}"
    try:
        dest_parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  couldn't create {dest_parent}: {e}; leaving album in staging."))
        return
    moved = 0
    for d in album_dirs:
        if not d.exists():
            continue
        try:
            shutil.move(str(d), str(dest_parent / d.name))
            moved += 1
        except OSError as e:
            log.info(fmt(C.YELLOW,
                f"  ⚠  couldn't move {d.name} to {dest_parent}: {e}"))
    if moved:
        log.info(fmt(C.YELLOW,
            f"  ⏭  parked {moved} album dir(s) at "
            f"{dest_parent.relative_to(cfg.STAGING_DIR)}/ "
            f"— retry later with: beet import {dest_parent}"))


def _resolve_queue_item(item, args, imported_globally):
    """Resolve backup and compute result for one completed queue item.

    Handles art cleanup, split-folder merge, sibling deletion, and backup
    restoration. Mutates item["imported"] and item["_resolved_post_dir"].
    Returns a result dict in process_album's shape.
    """
    item["imported"] = imported_globally
    bp = item.get("backup_path")
    album_dir = item["album_dir"]

    # One strict per-album success flag — art cleanup, sibling deletion,
    # backup resolution, and migration must all agree. A partial result
    # (e.g. 5/12 tracks ok) must not delete backups or siblings.
    _item_strict_success = (
        imported_globally
        and item.get("n_ok", 0) > 0
        and item.get("n_fail", 0) == 0
        and item.get("n_lossy", 0) == 0
    )
    if (getattr(args, "migrate_multi_artist", False) and _item_strict_success):
        _migrated = prompt_and_migrate_multi_artist_folder(item["album"], args)
    else:
        _migrated = None
    post_dir = _migrated or find_album_dir_filesystem(item["album"]) or album_dir
    # For brand-new albums (album_dir=None), find_album_dir_filesystem may
    # return None if the cache hasn't refreshed. Clear and retry once.
    if post_dir is None:
        clear_scan_caches()
        post_dir = find_album_dir_filesystem(item["album"])
    album_has_content = (
        post_dir is not None and post_dir.exists()
        and _count_audio_files_in(post_dir) > 0
    )
    # Stash resolved post-import dir so the lyric-retry resolver can
    # use it without redoing the find_album_dir_filesystem dance.
    item["_resolved_post_dir"] = post_dir
    had_any_success = (
        imported_globally and item.get("n_ok", 0) > 0 and album_has_content
    )

    if had_any_success:
        if post_dir:
            cleanup_duplicate_art(post_dir)
        # Split-folder auto-merge: a gap-fill against a folder beets doesn't
        # name canonically (multi-artist, or missing the year) makes it file
        # the new tracks elsewhere, splitting the album. Pull the old tracks
        # into the dir that now holds the fresh ones.
        try:
            split_artist = (item["album"].get("artist") or {}).get("name") or ""
            if _is_split_album_merge(album_dir, post_dir, split_artist):
                from qobuz_librarian.integrations.beets import _merge_split_folder
                n_merged = _merge_split_folder(post_dir, album_dir)
                if n_merged:
                    log.info(fmt(C.GREEN,
                        f"  ✓  Consolidated {n_merged} existing track(s) "
                        f"into {truncate(post_dir.name, 40)}"))
                else:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Split for {truncate(album_dir.name, 40)} "
                        f"but nothing merged (overlap conflicts in {album_dir})."))
        except Exception as _e_sf:
            vlog(f"split-folder merge raised: {_e_sf}")
        # Sibling deletion requires n_fail == 0 AND n_lossy == 0 — a partial
        # download must not wipe the sibling that may hold the missing tracks.
        if item.get("n_fail", 0) == 0 and item.get("n_lossy", 0) == 0:
            for sib_dir in item.get("siblings_to_delete", []):
                if sib_dir.exists():
                    try:
                        shutil.rmtree(sib_dir)
                        log.info(fmt(C.GRAY, f"  🗑  Removed sibling: {sib_dir.name}"))
                    except OSError as _e_sib:
                        log.info(fmt(C.YELLOW,
                            f"  ⚠  Couldn't remove {sib_dir.name}: {_e_sib}"))
        elif item.get("siblings_to_delete"):
            log.info(fmt(C.GRAY,
                f"  · Keeping {len(item['siblings_to_delete'])} sibling(s) "
                f"— partial result (n_fail={item.get('n_fail', 0)}, "
                f"n_lossy={item.get('n_lossy', 0)})"))

    if bp is not None:
        # Backup resolution uses stricter success than art/sibling cleanup —
        # the backup is the only intact copy of pre-upgrade content, so only
        # drop it when the new folder is whole (n_fail == 0 AND n_lossy == 0).
        n_fail_item = item.get("n_fail", 0)
        n_lossy_item = item.get("n_lossy", 0)
        per_album_success = had_any_success and n_fail_item == 0 and n_lossy_item == 0
        if per_album_success:
            try:
                shutil.rmtree(bp)
            except OSError as e:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Couldn't remove backup for {truncate(album_dir.name, 40)}: {e}"))
        elif args.no_import:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: backup kept at {bp}"))
        else:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: upgrade did not succeed; restoring..."))
            if restore_upgrade_backup(bp, album_dir):
                log.info(fmt(C.GREEN, f"  ✓  Restored {truncate(album_dir.name, 50)}"))
            else:
                log.info(fmt(C.RED, f"  ✗  Auto-restore failed. Backup: {bp}"))
                log.info(fmt(C.WHITE, f"     Manual: mv {bp} {album_dir}"))

    # Gap-fill backup: present tracks moved to backup before rip.
    # Drop on success; restore in place if the queue item failed.
    gfb = item.get("gap_fill_backup_path")
    if gfb is not None and gfb.exists():
        # Require zero lossy-deletes too: if a re-ripped present track came
        # back lossy and was deleted, the backed-up original must be restored,
        # not cleared (mirrors the full-album gap-fill gate in process.py).
        gap_fill_succeeded = (had_any_success
                              and item.get("n_fail", 0) == 0
                              and item.get("n_lossy", 0) == 0)
        if gap_fill_succeeded:
            try:
                shutil.rmtree(gfb)
            except OSError as e:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Gap-fill complete but couldn't remove backup: {e}"))
        else:
            _restore_target = album_dir or post_dir
            if _restore_target is None:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Gap-fill failed and no album dir to restore to. "
                    f"Backed-up tracks at: {gfb}"))
            else:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  {truncate(_restore_target.name, 40)}: "
                    f"gap-fill did not succeed; restoring backed-up tracks…"))
                _n_back = restore_gap_fill_backup(gfb, _restore_target)
                log.info(fmt(C.GREEN,
                    f"  ✓  Restored {_n_back} track(s) to "
                    f"{truncate(_restore_target.name, 50)}"))

    n_ok = item.get("n_ok", 0)
    n_fail = item.get("n_fail", 0)
    if item.get("result") == "interrupted":
        return {"dir": album_dir, "result": "interrupted"}
    if item.get("result") == "upgrade_aborted_backup_failed":
        return {"dir": album_dir, "result": "upgrade_aborted_backup_failed"}

    if n_ok and n_fail:
        status = "partial"
    elif n_ok:
        status = "downloaded"
    elif n_fail:
        status = "failed"
    else:
        status = "nothing_landed"

    log_fetch({
        "ts": datetime.now(timezone.utc).isoformat(),
        "album_id": item["album"].get("id"),
        "artist": (item["album"].get("artist") or {}).get("name"),
        "title": item["album"].get("title"),
        "result": status,
        "tracks_total": len((item["album"].get("tracks") or {}).get("items") or []),
        "tracks_downloaded": n_ok,
        "tracks_failed": n_fail,
        "tracks_lossy_deleted": item.get("n_lossy", 0),
        "failed_titles": item.get("failed_tracks", []),
        "lossy_titles": item.get("lossy_tracks", []),
        "imported": imported_globally,
        "auto_upgrade": item["auto_upgrade"],
        "elapsed_s": int(item.get("elapsed", 0)),
        "queued": True,
    })
    return {
        "dir": album_dir,
        "result": status,
        "n_ok": n_ok,
        "n_fail": n_fail,
        "n_lossy": item.get("n_lossy", 0),
        "imported": imported_globally,
        "auto_upgrade": item["auto_upgrade"],
    }


def _execute_download_queue(queue, args, token):
    """Flush a batch of pre-confirmed download decisions, one album at a time.

    Each item runs through its own pipeline — download → compress → lyrics →
    beets import (with retry on idle-timeout) → backup resolution — so a
    single broken or hung album in a 360-item batch loses only itself
    instead of the entire import. Albums that exhaust ``BEETS_MAX_ATTEMPTS``
    are parked under ``STAGING_DIR/<BEETS_RETRY_DIR>/`` so the rest of the
    queue keeps moving.

    Returns ``(results, beets_ok)``. ``beets_ok`` is True only when every
    album that landed audio also imported it, so callers that persist the
    queue on disk keep entries that didn't make it into the library.
    """
    if not queue:
        return [], True

    section(f"Download queue — {len(queue)} album(s)")

    if args.dry_run:
        log.info(fmt(C.YELLOW, "\n  --dry-run: would queue:"))
        dry_run_results = []
        for item in queue:
            tag = "↑ upgrade" if item["auto_upgrade"] else "fill"
            # Brand-new albums (missing-album / album-mode queue) have album_dir=None
            _dry_label = (item["album_dir"].name if item["album_dir"]
                          else (item["album"].get("title") or "?"))
            log.info(fmt(C.GRAY,
                f"    [{item['label']}] {tag}: {_dry_label}"))
            dry_run_results.append({"dir": item["album_dir"], "result": "dry_run",
                                    "n_missing": len(item["missing"])})
        return dry_run_results, True

    staging_preflight(args)
    interrupted = False
    cancelled = False
    auth_lost_exc = None
    queue_transient_lyric_sigs = []
    any_imported = False
    all_landed_imported = True   # False once any album that landed audio fails to import
    results = []

    def _short_circuit(item):
        """Drain an item we can no longer process (cancel / interrupt / auth
        loss): mark it, run backup resolution so any upgrade backup gets
        restored, and stash the result. Skipping resolution would orphan
        the original tracks under their backup path."""
        item.setdefault("snapshot_before", set())
        item.setdefault("result",
                        "cancelled" if cancelled
                        else "interrupted" if interrupted
                        else "auth_lost")
        results.append(_resolve_queue_item(item, args, False))

    for idx, item in enumerate(queue, 1):
        if interrupted or cancelled or auth_lost_exc is not None:
            _short_circuit(item)
            continue

        album = item["album"]
        album_dir = item["album_dir"]
        title = album.get("title") or "?"

        print()
        log.info(fmt(C.BOLD + C.WHITE,
            f"  [Q {idx}/{len(queue)}] {truncate(title, 55)}"))

        if item["auto_upgrade"] and album_dir and album_dir.exists():
            bp = backup_album_dir(album_dir)
            if bp is None:
                log.info(fmt(C.RED,
                    "    ✗  Could not back up; skipping this album."))
                item["result"] = "upgrade_aborted_backup_failed"
                results.append(_resolve_queue_item(item, args, False))
                continue
            item["backup_path"] = bp

        item["snapshot_before"] = snapshot_staging()
        try:
            _download_for_queue_item(item)
        except KeyboardInterrupt:
            log.info(fmt(C.YELLOW,
                "\n    Interrupted. Stopping further downloads — "
                "resolving backups for albums already processed."))
            item["result"] = "interrupted"
            interrupted = True
            results.append(_resolve_queue_item(item, args, False))
            continue
        except AuthLost as _e_auth:
            log.info(fmt(C.RED,
                "\n    Auth lost. Stopping further downloads — "
                "will restore upgrade backups and exit."))
            item["result"] = "auth_lost"
            auth_lost_exc = _e_auth
            results.append(_resolve_queue_item(item, args, False))
            continue

        if is_cancel_requested():
            from qobuz_librarian.modes.process import _discard_staged_since
            _discard_staged_since(item["snapshot_before"])
            item["result"] = "cancelled"
            cancelled = True
            log.info(fmt(C.YELLOW,
                "    Cancelled — discarded this album's partial download."))
            results.append(_resolve_queue_item(item, args, False))
            continue

        summary_n_ok = item.get("n_ok", 0)
        summary_n_fail = item.get("n_fail", 0)
        summary_elapsed = int(item.get("elapsed", 0))
        if summary_n_ok and not summary_n_fail:
            log.info(fmt(C.GREEN, f"    ✓ {summary_n_ok} track(s) · {summary_elapsed}s"))
        elif summary_n_ok:
            log.info(fmt(C.YELLOW,
                f"    ⚠ {summary_n_ok} ok · {summary_n_fail} failed · {summary_elapsed}s"))
        else:
            log.info(fmt(C.RED, f"    ✗ download failed · {summary_elapsed}s"))

        # ── Per-album pre-import + beets import ──────────────────────────
        item_imported = False
        if summary_n_ok > 0 and not args.no_import:
            album_dirs = _staged_album_dirs(item)
            if not album_dirs:
                log.info(fmt(C.YELLOW,
                    "    ⚠  No staged audio dir found for this album — skipping beets."))
            else:
                try:
                    sigs = _run_pre_import_hooks_for_dirs(album_dirs, args)
                    queue_transient_lyric_sigs.extend(sigs)
                except KeyboardInterrupt:
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Pre-import hook interrupted — skipping beets "
                        "for this album; stopping the queue."))
                    interrupted = True
                    results.append(_resolve_queue_item(item, args, False))
                    continue

                try:
                    item_imported = _import_album_with_retry(album_dirs)
                except KeyboardInterrupt:
                    log.info(fmt(C.YELLOW,
                        "\n  beets interrupted. Resolving backups based on disk."))
                    interrupted = True
                    item_imported = False
                    results.append(_resolve_queue_item(item, args, False))
                    continue

                if item_imported:
                    any_imported = True
                else:
                    all_landed_imported = False
                    _move_to_beets_retry(album_dirs, title)
        elif args.no_import:
            log.info(fmt(C.YELLOW,
                f"  --no-import: skipping beets. Files in {cfg.STAGING_DIR}/"))
            all_landed_imported = False
        elif summary_n_ok == 0:
            log.info(fmt(C.GRAY, "  Skipping beets — nothing landed."))

        results.append(_resolve_queue_item(item, args, item_imported))

        # Qobuz throttles sustained bulk queues. When the last rip showed
        # throttle signals, pause longer than the normal inter-album gap
        # so we stop pounding the limit.
        cooldown = cfg.RATE_LIMIT_COOLDOWN if item.get("rate_limited") else 0
        if cooldown and idx < len(queue):
            log.info(fmt(C.YELLOW,
                f"    ⏳ Qobuz rate-limit detected — cooling down "
                f"{int(cooldown)}s before the next album "
                f"(set RATE_LIMIT_COOLDOWN=0 to disable)."))
            time.sleep(cooldown)
        else:
            time.sleep(cfg.DELAY_BETWEEN)

    # ── Post-batch: consolidate duplicate albums once if anything landed.
    # The fold is a library-wide pass, so running it per-album would waste
    # work; once at end is enough.
    if any_imported:
        try:
            _consolidate_duplicate_albums()
        except Exception as _e_c:
            vlog(f"consolidate duplicate albums raised: {_e_c}")

    # ── Post-batch lyric-retry resolution.
    print()
    _post_dirs = [it.get("_resolved_post_dir") for it in queue
                  if it.get("_resolved_post_dir") is not None]
    if (queue_transient_lyric_sigs
            and any_imported
            and auth_lost_exc is None
            and not interrupted):
        try:
            resolved = _resolve_signatures_to_paths(
                queue_transient_lyric_sigs, _post_dirs)
            if resolved:
                _record_post_import_lyric_retry(resolved)
                vlog(f"lyric retry: queued {len(resolved)} "
                     f"post-import path(s) for next-launch retry")
        except Exception as _e_lr:
            vlog(f"lyric retry resolution failed: {_e_lr}")
        # Materialise .lrc sidecars next to the final renamed files
        # (no-op unless LYRICS_FORMAT is sidecar/both).
        try:
            write_post_import_sidecars(_post_dirs)
        except Exception as _e_sc:
            vlog(f"post-import sidecar write raised: {_e_sc}")
    elif (queue_transient_lyric_sigs
            and auth_lost_exc is None
            and not interrupted):
        # Some albums downloaded but none imported — files still in staging
        # (or parked under .beets_retry/). Record those paths so the
        # next-launch retry has a shot. Stale entries self-prune there.
        try:
            resolved = _resolve_signatures_to_paths(
                queue_transient_lyric_sigs, [cfg.STAGING_DIR])
            if resolved:
                _record_post_import_lyric_retry(resolved)
                vlog(f"lyric retry: no album imported; queued "
                     f"{len(resolved)} staging path(s) for next-launch retry")
        except Exception as _e_lr:
            vlog(f"lyric retry (staging fallback) failed: {_e_lr}")

    n_success = sum(1 for r in results
                    if r.get("result") in ("downloaded", "partial"))
    n_total_ok = sum(r.get("n_ok", 0) for r in results)
    n_total_fail = sum(r.get("n_fail", 0) for r in results)
    log.info(fmt(C.GREEN if n_total_fail == 0 else C.YELLOW,
        f"  ✓ Queue done: {n_success}/{len(queue)} albums OK · "
        f"{n_total_ok} track{'s' if n_total_ok != 1 else ''} downloaded · "
        f"{n_total_fail} failed"))

    if auth_lost_exc is not None:
        raise auth_lost_exc
    # Re-raising KeyboardInterrupt is what stops _flush_queue in modes 4/5
    # from reaching shared_queue.clear() and silently destroying every
    # queued decision the user gave.
    if interrupted:
        raise KeyboardInterrupt
    return results, bool(all_landed_imported)
