"""Downsample walk — shrink hi-res library files to CD rate.

Pure local housekeeping: no Qobuz lookup, so it runs without credentials. The
resample is irreversible (the hi-res original is overwritten in place, with no
re-download fallback), so the walk confirms per artist with default NO and each
file is decode-verified before it replaces anything.
"""
import sys

from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE, downsample_dir
from qobuz_librarian.library.downsample import scan_artist_for_downsample
from qobuz_librarian.library.scanner import clear_scan_caches, list_library_artists
from qobuz_librarian.ui_cli.colors import C, banner, fmt, format_size, truncate
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.ui_cli.prompts import _flush_stdin, confirm


def run_downsample_walk_mode(args):
    """Walk every artist, find albums stored above CD rate, and shrink the
    chosen ones in place.

    Artists with nothing to shrink are skipped silently (a \\r progress line
    overwrites itself while scanning). Each artist confirms separately, default
    NO; --dry-run lists what would shrink and changes nothing.
    """
    clear_scan_caches()
    banner("Downsample — shrink hi-res library files to CD rate")

    if not HAVE_DOWNSAMPLE:
        log.info(fmt(C.YELLOW,
            "  ⚠  Downsampling isn't available (needs ffmpeg and flac)."))
        return

    all_artists = list_library_artists()
    if not all_artists:
        log.info(fmt(C.YELLOW, "  ⚠  No artist directories found."))
        return

    log.info(fmt(C.YELLOW,
        "  ⚠  This rewrites hi-res files in place to 44.1/48kHz. The originals "
        "are not kept"))
    log.info(fmt(C.YELLOW,
        "     and there is no undo — re-downloading hi-res later would just "
        "reverse the saving."))
    if args.dry_run:
        log.info(fmt(C.GRAY, "  --dry-run: listing candidates only, nothing is changed."))
    log.info(fmt(C.GRAY, "  Ctrl-C to stop at any point."))

    # Auto-accept gate. Plain --yes deliberately does NOT cover this path — its
    # contract is that destructive prompts still ask — so the user opts in
    # explicitly here for an unattended run.
    auto_accept_all = False
    if not args.dry_run:
        try:
            _r = input(fmt(C.CYAN,
                "\n  Auto-accept every artist and run unattended? [y/N]: "
            )).strip().lower()
        except EOFError:
            _r = ""
        if _r in ("y", "yes"):
            auto_accept_all = True
            log.info(fmt(C.GREEN, "  ✓ Auto-accepting every artist. Walk away."))
    print()

    n = len(all_artists)
    n_scanned = 0
    n_albums_done = 0
    total_saved = 0
    total_errors = 0

    try:
        for i, artist_dir in enumerate(all_artists):
            artist_name = artist_dir.name
            _scan_line = f"  [{i + 1}/{n}] Scanning {truncate(artist_name, 46)}…"
            if sys.stdout.isatty():
                print(f"\r{_scan_line:<80}", end="", flush=True)

            candidates = scan_artist_for_downsample(artist_dir)
            n_scanned += 1
            if not candidates:
                continue

            print()
            print()
            n_albums = len(candidates)
            est_total = sum(c.est_saving for c in candidates)
            log.info(fmt(C.BOLD + C.WHITE, f"  {artist_name}"))
            log.info(fmt(C.MAGENTA,
                f"  {plural(n_albums, 'album')} above CD rate — "
                f"~{format_size(est_total)} reclaimable:"))
            print()
            for c in candidates:
                log.info(f"    • {fmt(C.WHITE, truncate(c.title, 48))}   "
                         f"{fmt(C.GRAY, c.detail)}")
            print()

            if args.dry_run:
                continue

            _flush_stdin()
            if not confirm(f"  Downsample {plural(n_albums, 'album')}?",
                           default_yes=False, auto_yes=auto_accept_all):
                log.info(fmt(C.GRAY, "  Skipped."))
                print()
                continue

            for j, c in enumerate(candidates, 1):
                print()
                log.info(fmt(C.BOLD + C.WHITE,
                    f"  [{j}/{n_albums}] {truncate(c.title, 55)}"))
                res = downsample_dir(c.album_dir, verbose=True,
                                     base_dir=c.album_dir, log=log.info)
                if res.get("resampled"):
                    n_albums_done += 1
                total_saved += res.get("saved_bytes", 0)
                total_errors += res.get("errors", 0)
            print()
    except KeyboardInterrupt:
        print()
        log.info(fmt(C.GRAY, "  Interrupted."))

    print()
    log.info(fmt(C.GREEN, "  ✓  Downsample walk complete."))
    log.info(fmt(C.GRAY,
        f"     Scanned {plural(n_scanned, 'artist')} — shrank "
        f"{plural(n_albums_done, 'album')}, reclaimed {format_size(total_saved)}."))
    if total_errors:
        log.info(fmt(C.YELLOW,
            f"     {plural(total_errors, 'file')} could not be downsampled "
            "(left unchanged)."))
