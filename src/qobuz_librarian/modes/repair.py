"""Album repair mode — ISRC-anchored refill of truncated FLACs.

"""
import shutil
import sys
from collections import Counter
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
from qobuz_librarian.api.search import get_album
from qobuz_librarian.library.backup import backup_gap_fill_files, restore_gap_fill_backup
from qobuz_librarian.library.catalog import (
    _paths_equal,
    _sync_beets_db_after_file_move,
    find_album_dir_filesystem,
    maybe_remove_empty_dir,
)
from qobuz_librarian.library.discovery import resolve_artist_dir
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
    read_album_dir,
)
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.executor import _execute_download_queue
from qobuz_librarian.repair_log import append_repair_log, scan_dir_for_isrc_repairs
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.errors import EXIT_AUTH, die
from qobuz_librarian.ui_cli.logging import log, vlog


def _format_mmss(secs):
    """Format a duration in seconds as m:ss (e.g. 163.4 → '2:43')."""
    s = max(0, int(round(float(secs or 0))))
    return f"{s // 60}:{s % 60:02d}"


def _norm_isrc(raw):
    return (raw or "").replace("-", "").upper().strip()


def _relocate_refilled_into_album_dir(album_dir, landed_dir, wanted_isrcs,
                                      before_paths, landed_was_new):
    """Move refilled tracks beets filed elsewhere back into album_dir.

    beets places imports by their tags, so a refilled EP/compilation/bonus
    track whose canonical Qobuz album differs from the folder being repaired
    lands in a separate folder instead of going home. Only newly-imported
    files (not in before_paths) whose ISRC we set out to refill are moved, so
    a pre-existing track that happens to share the recording is left alone.

    landed_was_new flags a folder beets created solely for this misfiled
    import — once emptied of audio it (and any stray cover art it picked up)
    is removed wholesale. A folder the user already owned keeps its art.
    Returns the number of files relocated.
    """
    if landed_dir is None or _paths_equal(landed_dir, album_dir):
        return 0
    moved = 0
    for et in read_album_dir(landed_dir):
        src = et.get("path") or ""
        if not src or src in before_paths:
            continue
        if _norm_isrc(et.get("isrc")) not in wanted_isrcs:
            continue
        dst = album_dir / Path(src).name
        if dst.exists():
            continue
        try:
            album_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(src, str(dst))
        except OSError as e:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Couldn't move refilled track into "
                f"{truncate(album_dir.name, 40)}: {e}"))
            continue
        _sync_beets_db_after_file_move(Path(src), dst)
        moved += 1
    if not moved:
        return 0
    log.info(fmt(C.GRAY,
        f"  ⤷  Returned {moved} refilled track(s) to "
        f"{truncate(album_dir.name, 40)}"))
    if landed_was_new and not read_album_dir(landed_dir):
        try:
            shutil.rmtree(landed_dir)
        except OSError:
            maybe_remove_empty_dir(landed_dir)
    else:
        maybe_remove_empty_dir(landed_dir)
    return moved


def _refills_present_in(album_dir, wanted_isrcs):
    """True once every refilled ISRC has a file back in album_dir."""
    if not wanted_isrcs:
        return True
    present = {_norm_isrc(et.get("isrc")) for et in read_album_dir(album_dir)}
    return wanted_isrcs.issubset(present)


def _refills_intact(album_dir, wanted_isrcs, token):
    """True only when none of the refilled ISRCs is still flagged truncated by
    a fresh duration scan. A presence check can't tell a complete refill from a
    short-but-decodable one — the exact failure repair exists to fix — so the
    rebuilt folder is re-verified against the same gate before the originals'
    backup is trusted as redundant. Caller guarantees wanted_isrcs is non-empty;
    AuthLost / QobuzUnavailable propagate so a transient outage can't read as
    "still truncated"."""
    try:
        scan = scan_dir_for_isrc_repairs(album_dir, token, deep=True)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception:
        return False
    still_truncated = {_norm_isrc(b.get("isrc"))
                       for b in scan["verified_truncated"]}
    return wanted_isrcs.isdisjoint(still_truncated)


def _prompt_library_album_for_repair(args, token):
    """Library-scoped picker for repair mode. Returns (artist_dir, album_dir).

    Only walks MUSIC_ROOT — no Qobuz album-level matching here. Earlier
    versions of this picker called find_qobuz_album_for_dir, which guesses
    the edition by folder-name similarity. That guess is what made repair
    mode unsafe: it could match the 1992 master against the 2011 remaster
    and propose to "fix" tracks that were actually fine for the version
    the user owns. The new repair flow matches per-file by ISRC instead,
    so this picker stays out of the catalog entirely.

    Returns (None, None) when the user cancels."""
    print()
    log.info(fmt(C.GRAY, "  Tip: '?' lists artists; '*' scans your whole library."))
    artist_dir = None
    while artist_dir is None:
        try:
            r = input(fmt(C.CYAN,
                "  Artist (blank/q to return, '*' for whole library): ")).strip()
        except EOFError:
            return None, None
        if not r or r.lower() in ("q", "quit", "exit"):
            return None, None
        if r in ("*", "all") or r.lower() == "library":
            # Sentinel: caller sweeps every album under MUSIC_ROOT.
            return "__ALL__", None
        if r == "?":
            artists = list_library_artists()
            if not artists:
                log.info(fmt(C.YELLOW, "  No artist directories found."))
                continue
            log.info(fmt(C.GRAY, f"  {len(artists)} artist(s) in library:"))
            for d in artists[:50]:
                log.info(fmt(C.GRAY, f"    • {d.name}"))
            if len(artists) > 50:
                log.info(fmt(C.GRAY, f"    ... and {len(artists) - 50} more"))
            continue
        artist_dir = resolve_artist_dir(r)
        if artist_dir is None:
            log.info(fmt(C.YELLOW,
                f"  ⚠  No artist matches {r!r} (try '?' for the list)."))

    albums = list_artist_album_dirs(artist_dir)
    if not albums:
        log.info(fmt(C.YELLOW,
            f"  ⚠  No album folders under {artist_dir.name}."))
        return None, None

    log.info(fmt(C.BOLD + C.CYAN,
        f"\n  Albums by {artist_dir.name} ({len(albums)}):"))
    for i, d in enumerate(albums, 1):
        log.info(fmt(C.WHITE, f"   {i:>3}.  {truncate(d.name, 72)}"))

    while True:
        try:
            r = input(fmt(C.CYAN,
                f"  Pick album (1-{len(albums)}, q to cancel): ")).strip()
        except EOFError:
            return None, None
        if not r or r.lower() in ("q", "quit", "exit"):
            return None, None
        try:
            idx = int(r)
        except ValueError:
            log.info(fmt(C.GRAY, "  Enter a number, or q."))
            continue
        if not 1 <= idx <= len(albums):
            log.info(fmt(C.GRAY,
                f"  Out of range — pick 1-{len(albums)}."))
            continue
        album_dir = albums[idx - 1]
        break

    return artist_dir, album_dir


def repair_album_dir(album_dir, verified_truncated, artist_name, args, token):
    """Back up truncated originals, ISRC-refill them, resolve the backup.

    Non-interactive and self-contained — the CLI repair mode and the web
    Repair flow both call this so there is one implementation of the
    risky part. Forces no-upgrade for the run so a quality delta can never
    escalate a surgical repair into a full wipe-and-replace. Auth lost
    before the backup is taken aborts with the originals untouched; auth
    lost mid-refill restores the backup before re-raising. Returns
    {"n_ok", "n_fail", "backup"}.
    """
    saved_no_upgrade = getattr(args, "no_upgrade", False)
    args.no_upgrade = True
    try:
        # ── Resolve the refill plan before touching any files ────────────
        # Everything Qobuz-side happens up front, while the truncated
        # originals are still in place. If the parent-album lookup hits a
        # transient outage the repair aborts cleanly with nothing moved,
        # rather than stranding the only copies in the backup dir.
        parent_ids = []
        for b in verified_truncated:
            aid = ((b["qobuz_track"].get("album") or {}).get("id"))
            if aid:
                parent_ids.append(aid)
        most_common_aid = (Counter(parent_ids).most_common(1)[0][0]
                           if parent_ids else None)

        album = None
        if most_common_aid is not None:
            try:
                album = get_album(most_common_aid, token)
            except QobuzError as e:
                # A genuine no-match is recoverable — the per-track ISRC
                # matches drive the refill, so fall back to a synthetic album
                # dict. AuthLost / QobuzUnavailable are not QobuzError; they
                # propagate and abort before anything is moved.
                log.info(fmt(C.GRAY,
                    f"  (parent-album lookup failed: {e}; "
                    "falling back to synthetic album dict)"))
                album = None
        if album is None:
            album = {
                "id": most_common_aid or "repair",
                "title": album_dir.name,
                "artist": {"name": artist_name},
                "tracks": {"items": []},
            }

        qobuz_tracks_full = (album.get("tracks") or {}).get("items") or []
        missing_tracks = [b["qobuz_track"] for b in verified_truncated]

        present_synth = [{} for _ in range(max(
            0, len(qobuz_tracks_full) - len(missing_tracks)))]

        # force_track_by_track: repair must replace exactly the truncated
        # tracks, never re-rip the whole album URL. The flag makes this
        # un-bypassable regardless of the executor's missing/total ratio.
        qi = _build_queue_item(
            album=album,
            album_dir=album_dir,
            label=f"{artist_name} — {album_dir.name}  [repair]",
            missing=missing_tracks,
            present=present_synth,
            upgrade_only=False,
            auto_upgrade=False,
            force_track_by_track=True,
        )

        # Snapshot the album beets is likely to file these into — a track's
        # canonical Qobuz album can differ from album_dir — so a freshly
        # refilled file can be told apart from tracks already living there.
        wanted_isrcs = {_norm_isrc(b.get("isrc")) for b in verified_truncated}
        wanted_isrcs.discard("")
        landed_pre = (find_album_dir_filesystem(album)
                      if most_common_aid is not None else None)
        before_paths = set()
        if landed_pre is not None and not _paths_equal(landed_pre, album_dir):
            before_paths = {et.get("path") for et in read_album_dir(landed_pre)}

        # ── Back up the truncated originals (plan in hand) ───────────────
        broken_paths = [b["path"] for b in verified_truncated]
        backup_path = backup_gap_fill_files(broken_paths, album_dir)
        if not backup_path:
            log.info(fmt(C.RED,
                "  ✗  Backup creation failed — aborting repair to "
                "preserve existing files."))
            return {"n_ok": 0, "n_fail": len(verified_truncated), "backup": None}
        log.info(fmt(C.GRAY,
            f"  ⟳  Moved {len(verified_truncated)} broken file(s) "
            f"to backup: {backup_path.name}"))

        try:
            _execute_download_queue([qi], args, token)
        except AuthLost:
            # Don't auto-restore: a multi-track refill may have already written
            # good replacements, and moving the truncated originals back would
            # clobber them (the same reasoning as the interrupt branch below).
            # Preserve the backup and point the user at it.
            if backup_path and backup_path.exists():
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Auth lost mid-refill — truncated originals preserved at:\n"
                    f"     {backup_path}\n"
                    f"     Re-run Repair once your token is refreshed, or restore "
                    f"them by hand."))
            raise
        except (KeyboardInterrupt, Exception):
            # Interrupted or an unexpected failure mid-refill. Don't
            # auto-restore: a partly-succeeded refill may already have
            # written good replacements, and moving the truncated originals
            # back would clobber them. Preserve the backup and point the
            # user at it so the album isn't silently left short its tracks
            # with no way to recover them.
            if backup_path and backup_path.exists():
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Repair interrupted — truncated originals preserved at:\n"
                    f"     {backup_path}\n"
                    f"     Re-run Repair to retry, or restore them by hand."))
            raise

        # ── Put the refill back where it belongs ─────────────────────────
        # beets files by tags, so a track whose canonical Qobuz album differs
        # from album_dir (EPs, compilations, bonus tracks) lands in a stray
        # folder. Move it home before judging success.
        landed_post = qi.get("_resolved_post_dir") or find_album_dir_filesystem(album)
        _relocate_refilled_into_album_dir(
            album_dir,
            Path(landed_post) if landed_post else None,
            wanted_isrcs, before_paths, landed_was_new=landed_pre is None)

        n_fail_final = qi.get("n_fail", 0)
        n_ok_final = qi.get("n_ok", 0)
        download_clean = (n_fail_final == 0 and n_ok_final > 0
                          and qi.get("imported", False))
        # "Back in place" = the refilled files returned to album_dir. Success
        # additionally requires they're verifiably NOT still truncated — a
        # presence check alone would let a short re-rip pass, and "no ISRC to
        # verify by" is unproven, not proven.
        back_in_place = download_clean and _refills_present_in(album_dir, wanted_isrcs)
        repaired = (back_in_place and bool(wanted_isrcs)
                    and _refills_intact(album_dir, wanted_isrcs, token))

        # ── Backup resolution ────────────────────────────────────────────
        if backup_path and backup_path.exists():
            if repaired:
                try:
                    shutil.rmtree(backup_path)
                except OSError:
                    pass
            elif back_in_place:
                # The re-downloaded tracks are physically back but couldn't be
                # verified as intact (still truncated, or no ISRC to check
                # against). Keep the originals — deleting the only other copy on
                # a presence check alone is how a bad re-rip silently replaces a
                # good-enough track with a worse one.
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Re-downloaded tracks couldn't be verified as intact; "
                    f"keeping the originals at:\n     {backup_path}"))
            elif n_fail_final > 0:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  {n_fail_final} track(s) failed to re-download. "
                    f"Truncated originals preserved at:\n     {backup_path}"))
            else:
                # Downloaded but the replacement never made it back into
                # album_dir (beets didn't import, or filed it where we
                # couldn't reclaim it). Restore so the album is at least back
                # to its pre-repair state instead of silently short a track.
                log.info(fmt(C.YELLOW,
                    "  ⚠  Refill didn't return to the album folder — "
                    "restoring truncated originals so it isn't left short."))
                if restore_gap_fill_backup(backup_path, album_dir):
                    log.info(fmt(C.GREEN,
                        "  ✓  Restored truncated originals; re-run Repair to retry."))
                else:
                    log.info(fmt(C.RED,
                        f"  ✗  Auto-restore failed. Backup at:\n     {backup_path}"))

        # ── Replaced-tracks log (only on a genuine, in-place repair) ──────
        if repaired:
            log_entries = [{
                "artist": artist_name,
                "album":  album_dir.name,
                "title":  b["title"],
            } for b in verified_truncated]
            if append_repair_log(log_entries):
                log.info(fmt(C.CYAN,
                    f"  📋  Logged {len(log_entries)} replaced track(s) — "
                    f"refresh these albums on any offline client:\n"
                    f"     {cfg.REPAIR_LOG_PATH}"))

        return {
            "n_ok": n_ok_final if repaired else 0,
            "n_fail": (n_fail_final if repaired
                       else max(n_fail_final, len(verified_truncated))),
            "imported": repaired,
            "backup": str(backup_path) if backup_path else None,
        }
    finally:
        args.no_upgrade = saved_no_upgrade


_REPAIR_AUTH_LOST = ("\n✗  Auth lost. Set QOBUZ_USER_AUTH_TOKEN, or open the "
                     "Settings page in the web UI to update your token.\n")


def _scan_report_repair(album_dir, artist_name, args, token, deep=True,
                        quiet=False):
    """Scan one album dir by ISRC, report, confirm, and repair.

    Returns "repaired" | "clean" | "skipped" | "failed". Raises AuthLost to
    the caller (it decides whether to abort the whole run/sweep). Shared by
    the single-album picker path and the whole-library sweep so both behave
    identically per album. A whole-library sweep passes deep=False so healthy
    tracks skip the per-track Qobuz lookup (fast); a single album stays deep
    so every track is verified.

    quiet=True (the sweep) prints nothing for a healthy album — no header, no
    "nothing to repair" — so a clean library doesn't bury its handful of real
    findings under a screen of per-album reports. The caller keeps its own
    progress line live; the report commits it only when there's something to
    show.
    """
    if not quiet:
        section(f"Repair scan — {truncate(album_dir.name, 60)}")
        log.info(fmt(C.GRAY,
            "  Resolving every FLAC by ISRC against Qobuz "
            "(no album-edition guessing) …"))

    scan = scan_dir_for_isrc_repairs(album_dir, token, deep=deep)
    verified_truncated = scan["verified_truncated"]
    verified_ok        = scan["verified_ok"]
    isrc_no_match      = scan["isrc_no_match"]
    no_isrc_tag        = scan["no_isrc_tag"]

    if quiet and not verified_truncated and not any(
            x.get("diagnostic") for x in no_isrc_tag):
        return "clean"
    if quiet:
        # Commit the caller's \r progress line and head the report.
        print()
        log.info(fmt(C.BOLD + C.WHITE,
            f"  {artist_name} — {truncate(album_dir.name, 50)}"))

    log.info(fmt(C.GRAY,
        f"  {verified_ok} verified ok  ·  "
        f"{len(verified_truncated)} verified truncated  ·  "
        f"{len(isrc_no_match)} ISRC has no Qobuz match  ·  "
        f"{len(no_isrc_tag)} no ISRC tag"))

    if isrc_no_match:
        log.info(fmt(C.GRAY,
            "\n  Skipped (ISRC tag present but no Qobuz match — "
            "Apple Music import or removed from Qobuz?):"))
        for x in isrc_no_match[:10]:
            log.info(fmt(C.GRAY,
                f"    • {truncate(x['title'], 50)}  "
                f"[isrc={x['isrc']}]"))
        if len(isrc_no_match) > 10:
            log.info(fmt(C.GRAY,
                f"    ... and {len(isrc_no_match) - 10} more"))
    if no_isrc_tag:
        log.info(fmt(C.GRAY,
            "\n  Skipped (no ISRC tag — can't verify recording "
            "identity, refusing to guess):"))
        for x in no_isrc_tag[:10]:
            diag = x.get("diagnostic")
            if diag:
                log.info(fmt(C.YELLOW,
                    f"    • {truncate(x['title'], 60)} — {diag}"))
            else:
                log.info(fmt(C.GRAY,
                    f"    • {truncate(x['title'], 60)}"))
        if len(no_isrc_tag) > 10:
            log.info(fmt(C.GRAY,
                f"    ... and {len(no_isrc_tag) - 10} more"))

    if not verified_truncated:
        if not quiet:
            log.info(fmt(C.GREEN,
                "\n  ✓  No verified-truncated tracks. Nothing to repair."))
        return "clean"

    log.info(fmt(C.YELLOW + C.BOLD,
        f"\n  ⚠  {len(verified_truncated)} truncated file(s) "
        "(ISRC-verified, safe to repair):"))
    log.info(fmt(C.GRAY,
        "    track  title                                              "
        "  on-disk / Qobuz   ISRC"))
    for b in verified_truncated:
        qt = b["qobuz_track"]
        title = b["title"]
        ver = qt.get("version") or ""
        if ver and ver.lower() not in title.lower():
            title = f"{title} ({ver})"
        tnum = b["track_number"]
        tnum_str = f"#{tnum:>2}" if tnum else "   "
        log.info(
            fmt(C.WHITE, f"    {tnum_str}  {truncate(title, 50):<50}")
            + fmt(C.GRAY,
                f"   {_format_mmss(b['file_length']):>5}"
                f" / {_format_mmss(b['qobuz_duration']):>5}"
                f"  {b['isrc']}"))

    if not args.yes:
        try:
            r = input(fmt(C.CYAN,
                f"\n  Re-download {len(verified_truncated)} "
                "ISRC-verified track(s)? [y/N]: ")
            ).strip().lower()
        except EOFError:
            r = ""
        if r not in ("y", "yes"):
            log.info(fmt(C.GRAY, "  Skipped."))
            return "skipped"

    result = repair_album_dir(album_dir, verified_truncated, artist_name, args, token)
    if result and result.get("n_ok", 0) > 0 and result.get("imported"):
        return "repaired"
    return "failed"


def _all_library_album_dirs():
    """Every (artist_dir, album_dir) under MUSIC_ROOT, artist-sorted."""
    pairs = []
    for adir in list_library_artists():
        for aldir in list_artist_album_dirs(adir):
            pairs.append((adir, aldir))
    return pairs


def run_album_repair_mode(args, token, *, loop=False):
    """ISRC-anchored refill of truncated FLACs — replaces are matched on
    ISRC so the new file is the same recording as the old one.

    `'*'` at the artist prompt sweeps the whole library. --no-upgrade is
    forced on so the upgrade path can't escalate to wipe-and-replace.
    """
    saved_no_upgrade = getattr(args, "no_upgrade", False)
    args.no_upgrade = True  # repair = surgical; never silently upgrade the rest

    try:
        while True:
            clear_scan_caches()
            try:
                artist_dir, album_dir = _prompt_library_album_for_repair(
                    args, token)
            except AuthLost:
                die(fmt(C.RED, _REPAIR_AUTH_LOST), EXIT_AUTH)

            if artist_dir == "__ALL__":
                targets = _all_library_album_dirs()
                if not targets:
                    log.info(fmt(C.YELLOW,
                        "  ⚠  No album folders found under the music root."))
                    if not loop:
                        return
                    continue
                section(f"Repair scan — whole library "
                        f"({len(targets)} album(s))")
                log.info(fmt(C.GRAY,
                    "  Albums with no damaged tracks are skipped silently. "
                    "Ctrl-C to stop."))
                tally = {"repaired": 0, "clean": 0, "skipped": 0, "failed": 0}
                try:
                    for i, (adir, aldir) in enumerate(targets, 1):
                        _line = (f"  [{i}/{len(targets)}] Scanning "
                                 f"{truncate(adir.name, 28)} — "
                                 f"{truncate(aldir.name, 30)}…")
                        if sys.stdout.isatty():
                            print(f"\r{_line:<90}", end="", flush=True)
                        else:
                            vlog(_line.strip())
                        try:
                            status = _scan_report_repair(
                                aldir, adir.name, args, token,
                                deep=False, quiet=True)
                        except AuthLost:
                            print()
                            die(fmt(C.RED, _REPAIR_AUTH_LOST), EXIT_AUTH)
                        except QobuzUnavailable:
                            # Finish the progress line before the transient
                            # abort propagates to the clean EXIT_TRANSIENT stop.
                            print()
                            raise
                        tally[status] = tally.get(status, 0) + 1
                except KeyboardInterrupt:
                    print()
                    log.info(fmt(C.YELLOW,
                        "  Interrupted — stopping library repair sweep."))
                else:
                    if sys.stdout.isatty():
                        print(f"\r{' ' * 90}\r", end="", flush=True)
                section("Library repair summary")
                _summary = (f"  repaired: {tally['repaired']}  ·  "
                            f"clean: {tally['clean']}  ·  "
                            f"skipped: {tally['skipped']}")
                if tally["failed"]:
                    _summary += f"  ·  failed: {tally['failed']}"
                log.info(fmt(C.GRAY, _summary))
                if not loop:
                    return
                continue

            if album_dir is None:
                return

            try:
                _scan_report_repair(album_dir, artist_dir.name, args, token)
            except AuthLost:
                die(fmt(C.RED, _REPAIR_AUTH_LOST), EXIT_AUTH)

            if not loop:
                return
    finally:
        args.no_upgrade = saved_no_upgrade
