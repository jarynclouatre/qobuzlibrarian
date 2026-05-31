"""Upgrade walk mode — scan every artist for quality upgrade candidates.

"""
import sys
import time

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzUnavailable
from qobuz_librarian.library.catalog import album_year, find_existing_tracks
from qobuz_librarian.library.scanner import clear_scan_caches, list_library_artists
from qobuz_librarian.library.tags import VA_NORMALIZED, normalize
from qobuz_librarian.modes.process import process_album
from qobuz_librarian.quality.decision import (
    compare_album_quality,
    is_album_capped,
    load_capped,
    mark_album_capped,
    scan_artist_for_upgrades,
)
from qobuz_librarian.ui_cli.colors import C, banner, fmt, truncate
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import _flush_stdin, confirm

# process_album outcomes that mean "nothing to upgrade here", not a failure.
# A backup-failed abort is deliberately absent: that's a real failure (Qobuz
# would have had an upgrade, but the existing folder couldn't be safely backed
# up) and the user should see it counted. The web upgrade flow imports this so
# the CLI walk and the web both classify results the same way.
BENIGN_UPGRADE_RESULTS = frozenset({
    "upgrade_only_no_op",
    "skipped_already_higher_quality",
    "skipped_has_extras",
    "lossy_only",
    "no_tracks",
    "user_skipped",
    "dry_run",
    "cancelled",
})


def run_upgrade_walk_mode(args, token):
    """Upgrade walk — walk every artist in the library, find albums that can
    be quality-upgraded, and prompt once per artist (default YES = enter).

    Artists with no upgradeable albums are skipped silently. A \\r progress
    line overwrites itself while scanning so the output stays clean — the
    screen only fills when real upgrades are found. After a Y/n answer the
    walk automatically advances to the next artist with no further prompts.
    """
    clear_scan_caches()
    banner("Upgrade walk — quality upgrades across library")

    all_artists = list_library_artists()
    if not all_artists:
        log.info(fmt(C.YELLOW, "  ⚠  No artist directories found."))
        return

    # Various Artists / VA can't be meaningfully matched on Qobuz.
    all_artists = [a for a in all_artists
                   if normalize(a.name) not in VA_NORMALIZED]
    if not all_artists:
        log.info(fmt(C.YELLOW, "  ⚠  No scannable artist directories found."))
        return

    # Consolidation prompts per-album would be unbearably noisy at scale.
    saved_consolidate = args.consolidate
    args.consolidate = False

    n = len(all_artists)
    n_scanned = 0
    n_upgraded_albums = 0
    n_unverified_albums = 0
    n_failed_albums = 0
    unsafe_artists = []  # --auto-safe skipped artists, for end-of-run review

    # Load cap markers; surface count for transparency.
    capped = load_capped()
    if capped:
        active = sum(1 for aid in capped if is_album_capped(aid, capped))
        if active:
            log.info(fmt(C.GRAY,
                f"  ⓘ {active} album(s) marked upgrade-capped will be skipped."))
            log.info(fmt(C.GRAY,
                f"     (Qobuz delivered partial hi-res before; "
                f"retry after {cfg.CAPPED_RETENTION_DAYS}d or edit {cfg.CAPPED_FILE.name}.)"))

    log.info(fmt(C.GRAY, f"  {plural(n, 'artist')} to scan. Artists with no upgrades are skipped silently."))
    log.info(fmt(C.GRAY, "  Ctrl-C to stop at any point."))

    # Auto-accept-all gate.
    auto_accept_all = False
    if not args.yes and not getattr(args, "auto_safe", False):
        try:
            _r = input(fmt(C.CYAN,
                "\n  Auto-accept all upgrades and run unattended? [y/N]: "
            )).strip().lower()
        except EOFError:
            _r = ""
        if _r in ("y", "yes"):
            auto_accept_all = True
            log.info(fmt(C.GREEN,
                "  ✓ Auto-accepting every artist. Walk away."))
    log.info("")

    try:
        for i, artist_dir in enumerate(all_artists):
            artist_name = artist_dir.name

            _scan_line = f"  [{i + 1}/{n}] Scanning {truncate(artist_name, 46)}…"
            # The \r overwrite only makes sense on a terminal; piped/log output
            # would just collect carriage returns, so keep it verbose-only there.
            if sys.stdout.isatty():
                print(f"\r{_scan_line:<80}", end="", flush=True)
            else:
                vlog(_scan_line.strip())

            try:
                candidates = scan_artist_for_upgrades(
                    artist_name, artist_dir, token, args, capped=capped)
            except (AuthLost, QobuzUnavailable):
                # Finish the progress line before the abort propagates to the
                # clean EXIT_AUTH / EXIT_TRANSIENT stop in the entry point.
                log.info("")
                raise
            except KeyboardInterrupt:
                log.info("")
                log.info(fmt(C.GRAY, "\n  Interrupted."))
                break

            n_scanned += 1

            if not candidates:
                continue

            # Upgrades found — commit the progress line and print the summary.
            log.info("")
            log.info("")
            n_tracks = sum(c["n_present"] for c in candidates)
            n_albums = len(candidates)

            log.info(fmt(C.BOLD + C.WHITE, f"  {artist_name}"))
            log.info(fmt(C.MAGENTA,
                f"  {plural(n_albums, 'album')} — {plural(n_tracks, 'track')} to re-rip:"))
            log.info("")
            for c in candidates:
                album = c["qobuz_album"]
                title = truncate(album.get("title") or "?", 48)
                year  = album_year(album) or "?"
                el    = c["existing_quality_label"]
                tl    = c["target_quality_label"]
                _np   = c.get("n_present", 0)
                _nt   = c.get("n_total", 0)
                _nb   = c.get("n_below", 0)
                _suffix = ""
                if _nt and _np < _nt:
                    _suffix += fmt(C.GRAY, f"   [partial: {_np} of {_nt}]")
                if 0 < _nb < _np:
                    _suffix += fmt(C.GRAY, f"   ({_nb} below target)")
                log.info(f"    • {fmt(C.WHITE, title)} {fmt(C.GRAY, f'({year})')}   "
                         f"{fmt(C.GRAY, el)} {fmt(C.GRAY, '→')} "
                         f"{fmt(C.MAGENTA, tl)}{_suffix}")
            log.info("")

            # --auto-safe — fully unattended path.
            if getattr(args, "auto_safe", False):
                low_conf = []
                for _c in candidates:
                    _reasons = []
                    if _c.get("_needed_edition_swap"):
                        _reasons.append("edition was auto-swapped")
                    _sim = _c.get("_title_similarity") or 0.0
                    if _sim < cfg.AUTO_SAFE_TITLE_SIM_THRESH:
                        _reasons.append(
                            f"title similarity {_sim:.2f} < "
                            f"{cfg.AUTO_SAFE_TITLE_SIM_THRESH:.2f}")
                    if _reasons:
                        low_conf.append(
                            (_c["qobuz_album"].get("title") or "?", _reasons))
                if low_conf:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  --auto-safe: skipping {artist_name} "
                        f"({len(low_conf)}/{len(candidates)} low-confidence)."))
                    for _t, _rs in low_conf:
                        log.info(fmt(C.GRAY,
                            f"     · {truncate(_t, 50)}  —  {'; '.join(_rs)}"))
                    unsafe_artists.append((artist_name, low_conf))
                    log.info("")
                    continue
                log.info(fmt(C.GREEN,
                    f"  ✓  --auto-safe: all {n_albums} candidate(s) high-confidence."))
            else:
                _flush_stdin()
                if not confirm(f"  Upgrade {plural(n_albums, 'album')}?",
                               default_yes=False,
                               auto_yes=args.yes or auto_accept_all):
                    log.info(fmt(C.GRAY, "  Skipped."))
                    log.info("")
                    continue

            for j, c in enumerate(candidates, 1):
                album = c["qobuz_album"]
                label = f"[{j}/{n_albums}]"
                log.info("")
                log.info(fmt(C.BOLD + C.WHITE,
                    f"  {label} {truncate(album.get('title') or '?', 55)}"))
                try:
                    # Upgrade_only=True so partial-album cases
                    # only re-rip the present tracks, not download missing ones.
                    _proc_result = process_album(album, args, allow_force=False,
                                  label=label, already_confirmed=True,
                                  upgrade_only=True,
                                  token=token)
                except AuthLost:
                    raise
                except KeyboardInterrupt:
                    log.info("")
                    log.info(fmt(C.GRAY, "  Interrupted. Stopping upgrade walk."))
                    return

                _pr_result = (_proc_result or {}).get("result", "")
                if _pr_result in BENIGN_UPGRADE_RESULTS:
                    time.sleep(cfg.ARTIST_API_DELAY)
                    continue
                if (_proc_result or {}).get("upgrade_unverified", False):
                    # Imported, but the rebuilt folder couldn't be verified as
                    # complete as the original, so the backup was kept. Tally it
                    # apart — it's neither a clean upgrade nor an outright fail.
                    n_unverified_albums += 1
                    time.sleep(cfg.ARTIST_API_DELAY)
                    continue
                if not (_proc_result or {}).get("imported", False):
                    # Attempted (Qobuz had a higher-quality copy) but didn't
                    # land — backup failed, download failed, import failed.
                    # process_album already logged the reason; tally it so the
                    # end-of-run summary doesn't imply a clean sweep.
                    n_failed_albums += 1
                    time.sleep(cfg.ARTIST_API_DELAY)
                    continue
                n_upgraded_albums += 1

                # Post-upgrade verification.
                try:
                    post_existing, _ = find_existing_tracks(album)
                    if post_existing:
                        post_qual = compare_album_quality(post_existing, album)
                        if post_qual["classification"] in ("all_lower", "mixed_below"):
                            mark_album_capped(album.get("id"), album, post_qual)
                            capped = load_capped()
                            log.info(fmt(C.YELLOW,
                                f"     ⚠  Upgrade incomplete: "
                                f"{post_qual['n_below']} track(s) still below target. "
                                f"Marked capped (Qobuz partial hi-res)."))
                except Exception as _e:
                    vlog(f"post-upgrade cap check failed: {_e}")
                time.sleep(cfg.ARTIST_API_DELAY)

            log.info("")

    finally:
        args.consolidate = saved_consolidate

    log.info("")
    log.info(fmt(C.GREEN, "  ✓  Upgrade walk complete."))
    log.info(fmt(C.GRAY,
        f"     Scanned {plural(n_scanned, 'artist')} — upgraded tracks in "
        f"{plural(n_upgraded_albums, 'album')}."))
    if n_unverified_albums:
        log.info(fmt(C.YELLOW,
            f"     ⚠  {plural(n_unverified_albums, 'album')} kept the original "
            "(upgrade couldn't be verified complete — backup retained)."))
    if n_failed_albums:
        log.info(fmt(C.YELLOW,
            f"     ⚠  {plural(n_failed_albums, 'album')} couldn't be upgraded "
            "(see the log above)."))
    if unsafe_artists:
        log.info("")
        log.info(fmt(C.YELLOW + C.BOLD,
            f"  ⚠  {plural(len(unsafe_artists), 'artist')} skipped for manual review:"))
        for _a, _lc in unsafe_artists:
            log.info(fmt(C.WHITE, f"     {_a}"))
            for _t, _rs in _lc:
                log.info(fmt(C.GRAY,
                    f"       · {truncate(_t, 50)}  —  {'; '.join(_rs)}"))
        log.info(fmt(C.GRAY,
            "     Re-run without --auto-safe to review these interactively."))
