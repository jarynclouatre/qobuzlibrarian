"""Download phase for one album: pick a strategy, rip, drop lossy/broken
files, retry the strays once, and reconcile the counts.

Shared by the single-album path (`modes/process.py`) and the queue executor
(`queue/executor.py`). Both hand it a staging snapshot and the missing/present
split and read back the `n_ok`/`n_fail`/`n_lossy` bookkeeping. Results are
written into a caller-owned `result` dict as the work progresses — not just
returned — so the gap-fill backup taken mid-download can still be resolved by
the caller's finally/except when a rip raises AuthLost or hits a full disk.
"""
import re
import time

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthLost,
    detect_auth_lost,
    detect_disk_full,
    detect_rate_limited,
)
from qobuz_librarian.integrations.rip import (
    cleanup_lossy,
    files_added_since,
    is_cancel_requested,
    rip_url,
    snapshot_staging,
)
from qobuz_librarian.library.backup import backup_gap_fill_files
from qobuz_librarian.library.catalog import find_extras_in_existing
from qobuz_librarian.library.scanner import read_album_dir
from qobuz_librarian.library.tags import normalize, strip_edition_suffix
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.logging import log, report_progress, vlog


def match_key_from_stem(p):
    """Normalized title key from a filename stem (or bare stem string) used to
    line a downloaded/deleted file up against its Qobuz track.

    Accepts a Path or a string: cleanup_lossy hands back ``f.stem`` strings, and
    a Path's ``.stem`` would mis-split a title like "01. ★" (pathlib reads ". ★"
    as a suffix), so strings are taken verbatim. Strips a leading
    "<disc>-<track>"/"<track>" number and any "Artist - " prefix streamrip
    writes, then runs the result through the same normalize/strip_edition_suffix
    a Qobuz title goes through, so the two sides compare on equal terms."""
    s = p.stem if hasattr(p, "stem") else str(p)
    m = re.match(r"^(?:\d+[-.])?\d+[\s\-–—.]+(.+)$", s)
    t = m.group(1) if m else s
    m = re.match(r"^.+?\s+-\s+(.+)$", t)
    return normalize(strip_edition_suffix(m.group(1) if m else t))


def _bare_title(title):
    return normalize(strip_edition_suffix(title or ""))


def run_album_download(*, album, missing, present, album_dir, snapshot,
                       existing=None, quality=None, upgrade_only=False,
                       force_track_by_track=False, result=None):
    """Download ``missing`` for one album and reconcile what actually landed.

    Picks a single full-album rip when most of the album is missing, else
    fetches track by track. ``existing`` is the on-disk track list (dicts with
    "path") used to stash already-owned tracks before a full-album re-rip;
    pass None to have it read from ``album_dir`` only if that branch is reached.

    Writes into ``result`` (created if None) as it goes — ``gap_fill_backup_path``
    the moment the backup is taken, then n_ok / n_fail / n_lossy /
    failed_tracks / lossy_tracks / rate_limited / elapsed / download_full_album /
    full_album_rc at the end — and returns it. Honours is_cancel_requested() to
    stop early; raises AuthLost on auth loss and OSError(ENOSPC) on a full disk
    for the caller to handle."""
    if result is None:
        result = {}
    result.setdefault("gap_fill_backup_path", None)

    qobuz_tracks = (album.get("tracks") or {}).get("items") or []
    n_tracks_total = len(qobuz_tracks)

    # Streamrip's track-URL path crashes with KeyError: 'body' on some tracks
    # (older catalog, edge metadata), so prefer the album URL when most of the
    # album is missing — beets merges any redundant duplicate of a present
    # track on import. Small gap-fills stay track-by-track. Repair pins
    # per-track no matter the ratio, so a tweak here can't turn a targeted
    # truncation-repair into a wipe-and-replace.
    if force_track_by_track:
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
    rate_limited = False

    if download_full_album:
        log.info(fmt(C.GRAY,
            f"  Strategy: full-album URL "
            f"({len(missing)} of {n_tracks_total} missing)"))
    else:
        why = ("forced per-track (repair)" if force_track_by_track
               else f"{len(missing)} of {n_tracks_total} missing")
        log.info(fmt(C.GRAY, f"  Strategy: per-track ({why})"))

    if download_full_album:
        url = f"https://play.qobuz.com/album/{album_id}"
        section("Downloading full album")
        report_progress("Downloading album", 0, 0,
                        f"{album.get('title') or '?'} · {n_tracks_total} tracks")
        vlog(f"  ⟳  {url}")
        # Move the already-present tracks to a backup before the rip so beets
        # doesn't create 'Foo.1.flac' duplicates on import, and so a rip
        # failure (network drop, Ctrl+C, auth loss) can't leave the user with
        # permanently lost tracks. The caller restores it if we don't fully
        # succeed; recording it now keeps that recovery reachable on a raise.
        if present and album_dir:
            ex = existing if existing is not None else read_album_dir(album_dir)
            extra_paths = {e["path"]
                           for e in find_extras_in_existing(qobuz_tracks, ex)}
            to_clear = [e for e in ex if e["path"] not in extra_paths]
            if to_clear:
                vlog(f"pre-download: backing up + removing {len(to_clear)} present "
                     f"track(s) to prevent .1.flac collisions")
                result["gap_fill_backup_path"] = backup_gap_fill_files(
                    [e["path"] for e in to_clear], album_dir)
        rc, out = rip_url(url, timeout=cfg.RIP_TIMEOUT, live_output=True,
                          quality=quality)
        full_album_rc = rc
        if detect_auth_lost(out):
            raise AuthLost("rip output contained auth-lost markers")
        if detect_disk_full(out):
            raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
        rate_limited = rate_limited or detect_rate_limited(out)
        # rip exits 0 even when it skipped tracks after persistent retries;
        # count the ERROR markers so a "succeeded" line can't hide a gap.
        n_errors = len(re.findall(
            r"^\s*(?:\[\d{2}:\d{2}:\d{2}\]\s*)?ERROR\b", out, re.MULTILINE))
        if rc != 0:
            log.info(fmt(C.RED, f"  ✗  rip exit {rc}; last 300 chars:"))
            log.info(fmt(C.GRAY, "  " + out[-300:].replace("\n", "\n  ")))
        elif n_errors:
            log.info(fmt(C.YELLOW,
                f"  ⚠  rip exit 0 but {n_errors} error(s) in output — "
                f"some tracks likely skipped (see summary below)."))
        else:
            log.info(fmt(C.GREEN, "  ✓  Download succeeded."))
    else:
        section("Downloading missing tracks")
        for i, t in enumerate(missing, 1):
            if is_cancel_requested():
                break
            tid = t.get("id")
            # Show the version + track number so an EP of same-titled remixes
            # doesn't render as N identical lines that look like a dup-download.
            ttl = t.get("title") or "?"
            ver = t.get("version") or ""
            if ver and ver.lower() not in ttl.lower():
                ttl = f"{ttl} ({ver})"
            tnum = t.get("track_number")
            tnum_prefix = f"#{tnum:>2} · " if tnum else ""
            log.info(fmt(C.BLUE, f"\n  [{i}/{len(missing)}]") +
                     f"  {fmt(C.WHITE, truncate(tnum_prefix + ttl, 60))}")
            report_progress("Downloading", i, len(missing), ttl)
            rc, out = rip_url(f"https://play.qobuz.com/track/{tid}",
                              timeout=cfg.RIP_TIMEOUT, quality=quality)
            if detect_auth_lost(out):
                raise AuthLost("rip output contained auth-lost markers")
            if detect_disk_full(out):
                raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
            rate_limited = rate_limited or detect_rate_limited(out)
            if rc == 0:
                log.info(fmt(C.GREEN, "    ✓ ok"))
            elif is_cancel_requested():
                # rip exited because we asked it to stop, not a real failure.
                break
            else:
                n_fail += 1
                # Store the BARE title — the post-download reconcile matches
                # failed_tracks against on-disk filename stems (bare), so the
                # display-augmented "Title (Version)" would never match and a
                # track that actually landed could never be un-failed.
                failed_tracks.append(t.get("title") or "?")
                if "KeyError: 'body'" in out:
                    log.info(fmt(C.RED,
                        "    ✗ streamrip KeyError on track endpoint "
                        "(known bug; usually works via album URL)."))
                else:
                    log.info(fmt(C.RED, f"    ✗ rip exit {rc}"))
                    log.info(fmt(C.GRAY, "      " + out[-200:].replace("\n", " ")))
            # Qobuz throttles sustained per-track pulls; when the last rip shows
            # throttle signals, pause longer before the next so we stop pounding
            # the limit (set RATE_LIMIT_COOLDOWN=0 to disable).
            cooldown = cfg.RATE_LIMIT_COOLDOWN if detect_rate_limited(out) else 0
            if cooldown and i < len(missing):
                log.info(fmt(C.YELLOW,
                    f"    ⏳ Qobuz rate-limit detected — cooling down "
                    f"{int(cooldown)}s before the next track."))
                time.sleep(cooldown)
            else:
                time.sleep(cfg.DELAY_BETWEEN)

    new_files = files_added_since(snapshot)
    audio_new = [f for f in new_files if f.suffix.lower() in cfg.AUDIO_EXTS]
    vlog(f"  {len(new_files)} new file(s) in staging ({len(audio_new)} audio)")
    kept, lossy, broken = cleanup_lossy(audio_new)
    n_ok = len(kept)

    # Both reject kinds get one per-track retry: a broken FLAC is usually a
    # transient glitch, and the album URL occasionally serves lossy for a track
    # the track URL has lossless. One retry per track — no recursion, no loop.
    # Skipped once a cancel is in flight so we don't fire rips the user stopped.
    discarded = lossy + broken
    if discarded and missing and not is_cancel_requested():
        discarded_norms = {match_key_from_stem(d) for d in discarded}
        retry_targets = [t for t in missing
                         if _bare_title(t.get("title")) in discarded_norms]
        if retry_targets:
            log.info(fmt(C.GRAY,
                f"  ↻  Retrying {len(retry_targets)} lossy/incomplete "
                "track(s) once via per-track URL"))
            retry_snapshot = snapshot_staging()
            for t in retry_targets:
                tid = t.get("id")
                if not tid:
                    continue
                rc, out = rip_url(f"https://play.qobuz.com/track/{tid}",
                                  timeout=cfg.RIP_TIMEOUT, quality=quality)
                if detect_auth_lost(out):
                    raise AuthLost("rip output contained auth-lost markers")
                if detect_disk_full(out):
                    raise OSError(28, f"No space left on device at {cfg.STAGING_DIR}")
                rate_limited = rate_limited or detect_rate_limited(out)
            retry_audio = [f for f in files_added_since(retry_snapshot)
                           if f.suffix.lower() in cfg.AUDIO_EXTS]
            retry_kept, _, _ = cleanup_lossy(retry_audio)
            if retry_kept:
                # Recovered tracks move to ok; drop them from whichever reject
                # bucket they were in so the summary doesn't re-list them.
                recovered = {match_key_from_stem(p) for p in retry_kept}
                lossy = [d for d in lossy if match_key_from_stem(d) not in recovered]
                broken = [d for d in broken if match_key_from_stem(d) not in recovered]
                kept = kept + retry_kept
                n_ok = len(kept)
                log.info(fmt(C.GREEN,
                    f"  ✓  Retry recovered {len(retry_kept)} track(s)"))

    # `n_lossy`/`lossy_tracks` stay the count and stems of everything discarded
    # (lossy + broken) — the album-whole gates and the reconciliation math key
    # off "did every track land as a clean FLAC", which both kinds fail.
    # broken_tracks carries the incomplete-download subset so the summary can
    # word the two cases honestly.
    lossy_tracks = lossy + broken
    n_lossy = len(lossy_tracks)

    # Reconcile counts against what's actually on disk: rip can exit non-zero
    # yet land FLACs (post-processing crash), or exit 0 yet silently drop a
    # track. Without this the summary and activity log mis-report failures.
    if not download_full_album and failed_tracks and kept:
        surviving = {match_key_from_stem(p) for p in kept}
        recovered = [t for t in failed_tracks if _bare_title(t) in surviving]
        if recovered:
            failed_tracks = [t for t in failed_tracks
                             if _bare_title(t) not in surviving]
            n_fail = max(0, n_fail - len(recovered))
            log.info(fmt(C.GRAY,
                f"  · {len(recovered)} track(s) landed despite a streamrip "
                f"post-processing error — counting as success."))

    if download_full_album and full_album_rc is not None:
        # A full-album rip re-downloads the WHOLE album URL (all n_tracks_total
        # tracks), including the already-present ones we moved to the gap-fill
        # backup — so n_ok (every clean FLAC that landed) is counted against the
        # total, NOT len(missing). Using len(missing) here let a present track's
        # re-rip failure clamp n_fail to 0, which would (a) read an incomplete
        # fill as clean and (b) let the executor drop the gap-fill backup or a
        # sibling that still holds the missing track. A lossy fallback counts
        # once in the lossy bucket, so n_ok + n_lossy + n_fail == tracks attempted.
        n_fail = max(0, n_tracks_total - n_ok - n_lossy)
        if n_fail > 0:
            surviving_norms = {match_key_from_stem(p) for p in kept}
            lossy_norms = {match_key_from_stem(stem) for stem in lossy_tracks}
            failed_tracks = [
                t.get("title") for t in missing
                if _bare_title(t.get("title")) not in surviving_norms
                and _bare_title(t.get("title")) not in lossy_norms
            ]
        else:
            failed_tracks = []
            if full_album_rc != 0 and n_ok > 0:
                log.info(fmt(C.GRAY,
                    f"  · {n_ok} track(s) landed despite rip exit "
                    f"{full_album_rc} (streamrip post-processing error)."))

    if lossy:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {len(lossy)} track(s) only available lossy on Qobuz "
            f"(no lossless for your tier — another source needed):"))
        for d in lossy[:5]:
            log.info(fmt(C.GRAY, f"     {d}"))
    if broken:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {len(broken)} track(s) downloaded incomplete and were "
            f"discarded (a re-run usually fixes these):"))
        for d in broken[:5]:
            log.info(fmt(C.GRAY, f"     {d}"))

    result.update({
        "n_ok": n_ok,
        "n_fail": n_fail,
        "n_lossy": n_lossy,
        "failed_tracks": failed_tracks,
        "lossy_tracks": lossy_tracks,
        "broken_tracks": broken,
        "rate_limited": rate_limited,
        "elapsed": time.time() - t_start,
        "download_full_album": download_full_album,
        "full_album_rc": full_album_rc,
    })
    return result
