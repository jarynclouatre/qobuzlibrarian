"""Scan / execute logic behind the web Artist and Library flows.

These wrap the same engine the CLI uses (catalog matching, gap detection,
process_album) but without any terminal prompts — a scan attaches review
candidates to the job, and execution runs over the candidates the user kept.
"""
import argparse
import time
from pathlib import Path

from qobuz_fetch import config as cfg
from qobuz_fetch.api.auth import AuthLost
from qobuz_fetch.api.search import (
    get_album,
    get_artist_albums,
    search_artists,
)
from qobuz_fetch.library.catalog import (
    album_quality_label,
    album_year,
    compute_missing,
    dedup_album_versions,
    filter_compilation_albums,
    filter_short_releases,
    find_existing_tracks,
    find_qobuz_album_for_dir,
)
from qobuz_fetch.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_fetch.library.tags import VA_NORMALIZED, normalize, similarity
from qobuz_fetch.ui_cli.errors import plural
from qobuz_fetch.ui_cli.logging import log


def build_args():
    """Namespace of CLI flags used by process_album and the artist/walk runners.

    `consolidate` is forced False: the web has no confirm() UI, so letting
    the engine scan for siblings it can't act on would waste time.
    """
    return argparse.Namespace(
        force=False, yes=True, dry_run=False, no_import=False,
        no_upgrade=False, no_downsample=False, no_compress=False,
        prefer_hires=cfg.PREFER_HIRES,
        consolidate=False,
        migrate_multi_artist=cfg.MIGRATE_MULTI_ARTIST,
        include_comps=False,
        include_singles=False,
        no_catalog=False,
        auto_safe=False,
        # auto_upgrade is request-scoped (the explicit Upgrade flow flips it
        # to True for one run) so the Settings page can keep reading the
        # underlying cfg.AUTO_UPGRADE_ENABLED without a mid-job racy flip.
        auto_upgrade=cfg.AUTO_UPGRADE_ENABLED,
        verbose=False,
    )


def resolve_artist(query, token):
    """Return (artist_id, artist_name) for the best match, or (None, None)."""
    try:
        results = search_artists(query, token, limit=cfg.ARTIST_LOOKUP_LIMIT)
    except AuthLost:
        raise
    except Exception as e:
        log.info(f"  artist search failed for '{query}': {e}")
        return None, None
    if not results:
        return None, None
    best = max(results, key=lambda a: similarity(a.get("name", ""), query))
    if similarity(best.get("name", ""), query) < cfg.ARTIST_NAME_THRESH:
        return None, None
    return best.get("id"), best.get("name")


def _missing_albums(artist_id, artist_name, token, partial_only=False):
    """Yield Qobuz album dicts that need attention — either entirely
    absent from the library, or present on disk with track-level gaps.
    Partial albums get a `_partial_missing_count` key set on the dict
    so the candidate detail can show 'gap-fill: N missing'.

    With partial_only, fully-missing albums are skipped — only on-disk
    albums with track gaps are yielded (the album-fill use case).
    """
    catalog, total = get_artist_albums(artist_id, token,
                                       limit=cfg.ARTIST_CATALOG_LIMIT)
    if total and total > len(catalog):
        log.info(f"  Qobuz lists {total} albums; scanning the first "
                 f"{len(catalog)}.")
    pairs = dedup_album_versions(catalog, prefer_hires=cfg.PREFER_HIRES)
    pairs = filter_compilation_albums(pairs, artist_name)
    pairs = filter_short_releases(pairs, cfg.MISSING_ALBUMS_MIN_TRACKS)
    for album, _n_versions in pairs:
        existing, _album_dir = find_existing_tracks(album)
        if not existing:
            if not partial_only:
                yield album
            continue
        # On-disk: check for track gaps. process_album re-derives the
        # gap when the user approves, so we don't need to thread the
        # missing set through the candidate — just count it for the UI.
        qobuz_tracks = (album.get("tracks") or {}).get("items") or []
        if not qobuz_tracks:
            # get_artist_albums returns tracks_count but not the track list,
            # so the gap check would be dead without materializing it. Only
            # paid for on-disk albums (fully-missing ones short-circuit above).
            try:
                full = get_album(album.get("id"), token)
            except AuthLost:
                raise
            except Exception:
                full = None
            qobuz_tracks = ((full or {}).get("tracks") or {}).get("items") or []
        if not qobuz_tracks:
            continue
        missing, _present = compute_missing(qobuz_tracks, existing)
        if missing:
            album["_partial_missing_count"] = len(missing)
            yield album


def _add_album_candidate(job, album, artist_name):
    partial_n = album.get("_partial_missing_count")
    if partial_n:
        detail = (f"{album_year(album) or '?'} · {album_quality_label(album)} · "
                  f"gap-fill: {partial_n} missing of "
                  f"{album.get('tracks_count') or '?'}")
    else:
        detail = (f"{album_year(album) or '?'} · {album_quality_label(album)} · "
                  f"{album.get('tracks_count') or '?'} tracks")
    job.add_candidate(
        kind="album",
        title=album.get("title") or "?",
        artist=artist_name,
        detail=detail,
        payload={"album_id": album.get("id")},
    )


# ── Scans ─────────────────────────────────────────────────────────────────────

def scan_artist(job, query, token):
    clear_scan_caches()
    log.info(f"Resolving artist '{query}'…")
    artist_id, artist_name = resolve_artist(query, token)
    if not artist_id:
        log.info(f"  No confident Qobuz match for '{query}'.")
        return
    log.info(f"  Matched: {artist_name}. Scanning catalog for gaps…")
    n = 0
    for album in _missing_albums(artist_id, artist_name, token):
        _add_album_candidate(job, album, artist_name)
        n += 1
    log.info(f"  {plural(n, 'missing album')} found for {artist_name}.")


def scan_library(job, token, partial_only=False):
    clear_scan_caches()
    artists = list_library_artists()
    if not artists:
        log.info("No artist folders found under MUSIC_ROOT.")
        log.info("  Expected layout: $MUSIC_ROOT/<Artist>/<Album (Year)>/<track>.flac")
        log.info("  Check that QF_MUSIC_DIR in your .env points at the right place.")
        return
    target = "track gaps in owned albums" if partial_only else "missing albums"
    log.info(f"Scanning {plural(len(artists), 'library artist')} for {target} ...")
    total = 0
    started = time.monotonic()
    for i, artist_dir in enumerate(artists, 1):
        if job.cancel_requested:
            log.info("Cancelled — stopping scan.")
            return
        name = artist_dir.name
        eta = _fmt_eta(started, i - 1, len(artists))
        log.info(f"  [{i}/{len(artists)}]{eta} {name}")
        artist_id, artist_name = resolve_artist(name, token)
        if not artist_id:
            continue
        try:
            for album in _missing_albums(artist_id, artist_name, token,
                                         partial_only=partial_only):
                _add_album_candidate(job, album, artist_name)
                total += 1
        except AuthLost:
            raise
        except Exception as e:
            log.info(f"    skipped {name}: {e}")
        time.sleep(cfg.ARTIST_API_DELAY)
    if partial_only:
        log.info(f"Done. {plural(total, 'album')} with track gaps across the library.")
    else:
        log.info(f"Done. {plural(total, 'missing album')} across the library.")


def _fmt_eta(started: float, done: int, total: int) -> str:
    """Return ' (eta: 1m 23s)' once we have at least one completed item;
    empty string otherwise."""
    if done < 1 or total <= done:
        return ""
    elapsed = time.monotonic() - started
    per = elapsed / done
    remaining_s = int(per * (total - done))
    if remaining_s < 60:
        return f" (eta: {remaining_s}s)"
    return f" (eta: {remaining_s // 60}m {remaining_s % 60}s)"


# ── Execute ───────────────────────────────────────────────────────────────────

def execute_albums(job, chosen, token):
    """Download each selected album via the normal process_album path."""
    from qobuz_fetch.modes.process import process_album

    # The web worker runs jobs back-to-back; a directory listing cached by
    # a previous job would otherwise be reused even though folders may
    # have moved since.
    clear_scan_caches()
    args = build_args()
    _benign = {"already_complete", "skipped_already_higher_quality", "dry_run",
               "user_skipped", "lossy_only", "no_tracks"}
    ok = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            remaining = len(chosen) - processed
            log.info(f"Cancelled — {ok} downloaded, {remaining} not started.")
            return
        processed = i
        album_id = cand["payload"].get("album_id")
        label = f"[{i}/{len(chosen)}] {cand.get('artist','')} — {cand['title']}"
        log.info(label)
        try:
            full = get_album(album_id, token)
        except Exception as e:
            log.info(f"  could not fetch album {album_id}: {e}")
            failed += 1
            continue
        try:
            result = process_album(full, args, allow_force=False,
                                   already_confirmed=True, token=token)
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        if result and result.get("imported") and result.get("n_ok", 0) > 0:
            ok += 1
        elif not (result and result.get("result") in _benign):
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    log.info(f"Finished — {ok}/{plural(len(chosen), 'album')} downloaded and imported.")
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} didn't finish — see the log."


# ── Upgrade flow ──────────────────────────────────────────────────────────────

def scan_upgrades(job, token):
    """Scan the library for albums Qobuz can serve at higher quality."""
    from qobuz_fetch.quality.decision import load_capped, scan_artist_for_upgrades

    clear_scan_caches()
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        log.info("No artist folders found under MUSIC_ROOT.")
        log.info("  Check that QF_MUSIC_DIR in your .env points at the right place.")
        return
    args = build_args()
    capped = load_capped()
    log.info(f"Scanning {plural(len(artists), 'artist')} for quality upgrades ...")
    total = 0
    started = time.monotonic()
    for i, artist_dir in enumerate(artists, 1):
        if job.cancel_requested:
            log.info("Cancelled — stopping scan.")
            return
        name = artist_dir.name
        eta = _fmt_eta(started, i - 1, len(artists))
        log.info(f"  [{i}/{len(artists)}]{eta} {name}")
        try:
            cands = scan_artist_for_upgrades(name, artist_dir, token, args,
                                             capped=capped)
        except AuthLost:
            raise
        except Exception as e:
            log.info(f"    skipped {name}: {e}")
            continue
        for c in cands:
            album = c["qobuz_album"]
            np_, nt = c.get("n_present", 0), c.get("n_total", 0)
            part = f" · {np_}/{nt} tracks" if nt and np_ < nt else ""
            job.add_candidate(
                kind="upgrade",
                title=album.get("title") or "?",
                artist=name,
                detail=f"{c.get('existing_quality_label','?')} → "
                       f"{c.get('target_quality_label','?')}{part}",
                payload={"candidate": c},
            )
            total += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    log.info(f"Done. {plural(total, 'upgradeable album')} found.")


def execute_upgrades(job, chosen, token):
    """Re-rip the present tracks of each chosen album at higher quality."""
    from qobuz_fetch.modes.process import process_album

    clear_scan_caches()
    args = build_args()
    # Explicit upgrade: enable the replace path for this run only, and turn
    # off per-album consolidation prompts (the CLI upgrade walk does the same).
    args.auto_upgrade = True
    args.consolidate = False
    # Outcomes that aren't a failure — the album just didn't need (or couldn't
    # safely take) an upgrade. A backup-failed abort is NOT here: that's a real
    # failure the user should see.
    _skip = {"upgrade_only_no_op", "skipped_already_higher_quality",
             "skipped_has_extras", "lossy_only", "no_tracks",
             "user_skipped", "dry_run", "cancelled"}
    ok = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            remaining = len(chosen) - processed
            log.info(f"Cancelled — {ok} upgraded, {remaining} not started.")
            break
        processed = i
        c = cand["payload"]["candidate"]
        album = c["qobuz_album"]
        log.info(f"[{i}/{len(chosen)}] {cand.get('artist','')} — "
                 f"{album.get('title') or '?'}")
        try:
            result = process_album(album, args, allow_force=False,
                                   already_confirmed=True,
                                   upgrade_only=True, token=token)
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        _res = (result or {}).get("result")
        if result and result.get("imported") and _res not in (
                _skip | {"upgrade_aborted_backup_failed"}):
            ok += 1
        elif _res not in _skip:
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    log.info(f"Finished — upgraded {ok}/{plural(len(chosen), 'album')}.")
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} couldn't be upgraded — see the log."


# ── Repair flow ───────────────────────────────────────────────────────────────

def scan_repairs(job, token):
    """Scan every album for ISRC-verified truncated FLACs."""
    from qobuz_fetch.repair_log import scan_dir_for_isrc_repairs

    clear_scan_caches()
    artists = list_library_artists()
    if not artists:
        log.info("No artist folders found under MUSIC_ROOT.")
        log.info("  Check that QF_MUSIC_DIR in your .env points at the right place.")
        return
    log.info(f"Scanning {plural(len(artists), 'artist')} for truncated files ...")
    total = 0
    started = time.monotonic()
    for i, artist_dir in enumerate(artists, 1):
        if job.cancel_requested:
            log.info("Cancelled — stopping scan.")
            return
        name = artist_dir.name
        album_dirs = list_artist_album_dirs(artist_dir)
        eta = _fmt_eta(started, i - 1, len(artists))
        log.info(f"  [{i}/{len(artists)}]{eta} {name} ({plural(len(album_dirs), 'album')})")
        for album_dir in album_dirs:
            if job.cancel_requested:
                log.info("Cancelled — stopping scan.")
                return
            try:
                scan = scan_dir_for_isrc_repairs(album_dir, token)
            except AuthLost:
                raise
            except Exception as e:
                log.info(f"    skipped {album_dir.name}: {e}")
                continue
            truncated = scan["verified_truncated"]
            if truncated:
                job.add_candidate(
                    kind="repair",
                    title=album_dir.name,
                    artist=name,
                    detail=f"{plural(len(truncated), 'truncated track')}",
                    payload={"album_dir": str(album_dir),
                             "artist_name": name,
                             "verified_truncated": truncated},
                )
                total += 1
            # Damaged files with no readable ISRC can't be surgically refilled
            # (no anchored Qobuz lookup). Identify the album from the folder
            # and offer a whole-album re-download instead — the user sees the
            # matched album in the review and confirms before it runs.
            suspicious = [e for e in scan.get("no_isrc_tag", [])
                          if e.get("diagnostic")]
            if suspicious:
                matched = find_qobuz_album_for_dir(album_dir, name, token)
                if matched and matched.get("id"):
                    m_title = matched.get("title") or album_dir.name
                    m_year = album_year(matched) or "?"
                    job.add_candidate(
                        kind="redownload",
                        title=album_dir.name,
                        artist=name,
                        detail=(f"{plural(len(suspicious), 'damaged file')} "
                                f"can't be verified by ID — re-download the whole "
                                f"album fresh as “{m_title}” ({m_year})"),
                        payload={"album_dir": str(album_dir),
                                 "artist_name": name,
                                 "album_id": matched.get("id"),
                                 "matched_title": m_title},
                    )
                    total += 1
                else:
                    for e in suspicious:
                        log.info(f"    ⚠ {album_dir.name} — "
                                 f"{e.get('title') or '?'}: {e['diagnostic']}; "
                                 "couldn't match this folder to a Qobuz album "
                                 "to re-download — check by hand.")
        time.sleep(cfg.ARTIST_API_DELAY)
    log.info(f"Done. {plural(total, 'album')} flagged.")


def _redownload_damaged_album(payload, token):
    """Re-fetch a whole album whose damaged file couldn't be ID-verified.

    The folder is moved aside first so beets imports a clean copy instead of
    colliding with the broken files (the --force path can't be used here: it
    needs an interactive deletion confirm the web has no way to answer). If
    the re-download doesn't complete, the original folder is moved back so the
    user is never left worse off.
    """
    import shutil as _shutil

    from qobuz_fetch.library.backup import (
        backup_album_dir,
        restore_upgrade_backup,
    )
    from qobuz_fetch.modes.process import process_album

    log.info("  The damaged file can't be verified by its ID, so the whole "
             "album is being re-downloaded fresh from Qobuz.")
    full = get_album(payload["album_id"], token)
    album_dir = Path(payload["album_dir"])
    backup = backup_album_dir(album_dir) if album_dir.exists() else None
    if album_dir.exists() and backup is None:
        log.info("  Couldn't safely move the existing folder out of the way, "
                 "so leaving this album alone to avoid damaging it. "
                 "See the log above.")
        return {"imported": False, "n_ok": 0, "result": "backup_failed"}
    try:
        result = process_album(full, build_args(), allow_force=False,
                               already_confirmed=True, token=token) or {}
    except Exception:
        if backup:
            restore_upgrade_backup(backup, album_dir)
        raise
    succeeded = bool(result.get("imported")) and result.get("n_ok", 0) > 0
    if backup:
        if succeeded:
            _shutil.rmtree(backup, ignore_errors=True)
        else:
            log.info("  Re-download didn't complete — restoring the original "
                     "album folder.")
            restore_upgrade_backup(backup, album_dir)
    return result


def execute_repairs(job, chosen, token):
    """Refill ISRC-verified truncated tracks, or re-download whole albums
    whose damage couldn't be ID-verified — depending on each candidate."""
    from pathlib import Path

    from qobuz_fetch.modes.repair import repair_album_dir

    clear_scan_caches()
    args = build_args()
    fixed = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            remaining = len(chosen) - processed
            log.info(f"Cancelled — {fixed} repaired, {remaining} not started.")
            return
        processed = i
        p = cand["payload"]
        log.info(f"[{i}/{len(chosen)}] {p['artist_name']} — {cand['title']}")
        try:
            if cand.get("kind") == "redownload":
                result = _redownload_damaged_album(p, token)
            else:
                result = repair_album_dir(Path(p["album_dir"]),
                                          p["verified_truncated"],
                                          p["artist_name"], args, token)
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        # Each chosen album was flagged as damaged, so anything that didn't end
        # up downloaded-and-imported is a real failure.
        if result and result.get("n_ok", 0) > 0 and result.get("imported"):
            fixed += 1
        else:
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    log.info(f"Finished — repaired {fixed}/{plural(len(chosen), 'album')}.")
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} couldn't be repaired — see the log."


def run_lyric_retry(job, token):
    """Retry lyric fetching for tracks queued from a previous failed run."""
    from qobuz_fetch.integrations.lyrics import (
        HAVE_LYRIC_FETCH,
        _refresh_lyric_retry,
        load_lyric_retry,
        lyric_fetch,
        save_lyric_retry,
    )

    paths = load_lyric_retry()
    if not paths:
        log.info("No tracks queued for lyric retry.")
        return

    if not HAVE_LYRIC_FETCH:
        log.info("lyric_fetch is unavailable — manifest preserved.")
        return

    existing = [Path(p) for p in paths if Path(p).exists()]
    dropped = len(paths) - len(existing)
    if dropped:
        log.info(f"{dropped} queued path(s) no longer on disk — skipping.")
    if not existing:
        save_lyric_retry([])
        log.info("All queued files are missing; manifest cleared.")
        return

    log.info(f"Retrying lyrics on {plural(len(existing), 'track')} ...")
    try:
        lyric_fetch.fetch_for_paths(
            existing, log=log,
            providers=cfg.LYRICS_PROVIDERS or None,
            lyrics_format=cfg.LYRICS_FORMAT,
            state_path=cfg.LYRIC_FETCH_STATE_FILE,
        )
    except Exception as e:
        log.info(f"Retry failed: {e} — manifest preserved.")
        return

    _refresh_lyric_retry(existing)
    remaining = load_lyric_retry()
    if remaining:
        log.info(f"{plural(len(remaining), 'track')} still unresolved — will retry next time.")
    else:
        log.info("All retried tracks resolved.")
