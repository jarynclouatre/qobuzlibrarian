"""Album repair mode — ISRC-anchored refill of truncated FLACs.

"""
import shutil
from collections import Counter

from qobuz_fetch import config as cfg
from qobuz_fetch.api.auth import AuthLost, QobuzError
from qobuz_fetch.api.search import get_album
from qobuz_fetch.library.backup import backup_gap_fill_files, restore_gap_fill_backup
from qobuz_fetch.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_fetch.modes.artist import resolve_artist_dir
from qobuz_fetch.queue.builder import _build_queue_item
from qobuz_fetch.queue.executor import _execute_download_queue
from qobuz_fetch.repair_log import append_repair_log, scan_dir_for_isrc_repairs
from qobuz_fetch.ui_cli.colors import C, fmt, section, truncate
from qobuz_fetch.ui_cli.errors import EXIT_AUTH, die
from qobuz_fetch.ui_cli.logging import log


def _format_mmss(secs):
    """Format a duration in seconds as m:ss (e.g. 163.4 → '2:43')."""
    s = max(0, int(round(float(secs or 0))))
    return f"{s // 60}:{s % 60:02d}"


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
    escalate a surgical repair into a full wipe-and-replace. Raises AuthLost
    (after restoring the backup) if auth drops mid-run. Returns
    {"n_ok", "n_fail", "backup"}.
    """
    saved_no_upgrade = getattr(args, "no_upgrade", False)
    args.no_upgrade = True
    try:
        # ── Backup the truncated originals before any deletion ───────────
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

        # ── Build the synthetic queue item ───────────────────────────────
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
            except (QobuzError, AuthLost) as e:
                if isinstance(e, AuthLost):
                    if backup_path and backup_path.exists():
                        restore_gap_fill_backup(backup_path, album_dir)
                    raise
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

        try:
            _execute_download_queue([qi], args, token)
        except AuthLost:
            if backup_path and backup_path.exists():
                log.info(fmt(C.YELLOW,
                    "  Auth lost; restoring truncated originals …"))
                restore_gap_fill_backup(backup_path, album_dir)
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

        # ── Backup resolution ────────────────────────────────────────────
        if backup_path and backup_path.exists():
            n_fail = qi.get("n_fail", 0)
            imported = qi.get("imported", False)
            if n_fail > 0:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  {n_fail} track(s) failed to re-download. "
                    f"Truncated originals preserved at:\n"
                    f"     {backup_path}"))
            elif not imported:
                # Downloads succeeded but beets didn't import — replacements
                # are stranded in staging and album_dir is missing the
                # tracks we moved out. Auto-restore so the album is at
                # least back to its pre-repair (truncated) state; user
                # can re-run repair from a clean baseline.
                log.info(fmt(C.YELLOW,
                    "  ⚠  Beets import did not succeed — restoring "
                    "truncated originals so the album isn't left short."))
                if restore_gap_fill_backup(backup_path, album_dir):
                    log.info(fmt(C.GREEN,
                        "  ✓  Restored truncated originals; re-run Repair to retry."))
                else:
                    log.info(fmt(C.RED,
                        f"  ✗  Auto-restore failed. Backup at:\n     {backup_path}"))
            else:
                try:
                    shutil.rmtree(backup_path)
                except OSError:
                    pass

        # ── Replaced-tracks log ──────────────────────────────────────────
        n_fail_final  = qi.get("n_fail", 0)
        n_ok_final    = qi.get("n_ok", 0)
        imported_final = qi.get("imported", False)
        if n_fail_final == 0 and n_ok_final > 0 and imported_final:
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

        return {"n_ok": n_ok_final, "n_fail": n_fail_final,
                "imported": imported_final,
                "backup": str(backup_path) if backup_path else None}
    finally:
        args.no_upgrade = saved_no_upgrade


_REPAIR_AUTH_LOST = ("\n✗  Auth lost. Set QOBUZ_USER_AUTH_TOKEN, or open the "
                     "Settings page in the web UI to update your token.\n")


def _scan_report_repair(album_dir, artist_name, args, token):
    """Scan one album dir by ISRC, report, confirm, and repair.

    Returns "repaired" | "clean" | "skipped". Raises AuthLost to the
    caller (it decides whether to abort the whole run/sweep). Shared by
    the single-album picker path and the whole-library sweep so both
    behave identically per album.
    """
    section(f"Repair scan — {truncate(album_dir.name, 60)}")
    log.info(fmt(C.GRAY,
        "  Resolving every FLAC by ISRC against Qobuz "
        "(no album-edition guessing) …"))

    scan = scan_dir_for_isrc_repairs(album_dir, token)
    verified_truncated = scan["verified_truncated"]
    verified_ok        = scan["verified_ok"]
    isrc_no_match      = scan["isrc_no_match"]
    no_isrc_tag        = scan["no_isrc_tag"]

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
            log.info(fmt(C.GRAY,
                f"    • {truncate(x['title'], 60)}"))
        if len(no_isrc_tag) > 10:
            log.info(fmt(C.GRAY,
                f"    ... and {len(no_isrc_tag) - 10} more"))

    if not verified_truncated:
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
                "ISRC-verified track(s)? [Y/n]: ")
            ).strip().lower()
        except EOFError:
            r = ""
        if r in ("n", "no"):
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


def run_album_repair_mode(args, token, *, query_args=None, loop=False):
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
                tally = {"repaired": 0, "clean": 0, "skipped": 0, "failed": 0}
                try:
                    for i, (adir, aldir) in enumerate(targets, 1):
                        log.info(fmt(C.CYAN,
                            f"\n  [{i}/{len(targets)}] {adir.name} — "
                            f"{truncate(aldir.name, 50)}"))
                        try:
                            status = _scan_report_repair(
                                aldir, adir.name, args, token)
                        except AuthLost:
                            die(fmt(C.RED, _REPAIR_AUTH_LOST), EXIT_AUTH)
                        tally[status] = tally.get(status, 0) + 1
                except KeyboardInterrupt:
                    log.info(fmt(C.YELLOW,
                        "\n  Interrupted — stopping library repair sweep."))
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
