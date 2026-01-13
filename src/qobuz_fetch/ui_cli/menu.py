"""Interactive session mode picker."""
from qobuz_fetch.ui_cli.colors import C, fmt
from qobuz_fetch.ui_cli.logging import log
from qobuz_fetch.ui_cli.sentinels import Mode


def interactive_session_mode():
    """Top-of-loop menu. Re-prompts on unrecognized input rather than
    falling through to album mode on a typo."""
    print()
    log.info(fmt(C.GRAY, "  Qobuz Librarian: download Qobuz albums and fill your library's gaps."))
    log.info(fmt(C.BOLD + C.CYAN, "  What would you like to do?"))
    log.info(fmt(C.WHITE, "    1) Search    — fetch one album (search or paste Qobuz URL)"))
    log.info(fmt(C.WHITE, "    2) Artist    — work through one artist's catalog"))
    log.info("")
    log.info(fmt(C.BOLD + C.GRAY, "  ── Library scans (sweep everything you own) ──"))
    log.info(fmt(C.WHITE, "    3) by artist         — decide y/N per artist, download each immediately"))
    log.info(fmt(C.WHITE, "    4) by artist (queue) — decide y/N per artist, download all at the end"))
    log.info(fmt(C.WHITE, "    5) by album          — decide y/N per incomplete album across all artists"))
    log.info("")
    log.info(fmt(C.WHITE, "    6) Repair    — refill truncated/partial FLACs (ISRC-verified)"))
    log.info(fmt(C.WHITE, "    7) Upgrade   — scan the whole library for better-quality versions"))
    log.info(fmt(C.WHITE, "    q) Quit"))
    while True:
        try:
            r = input(fmt(C.CYAN, "  Choice (Enter = 1): ")).strip().lower()
        except EOFError:
            return Mode.QUIT
        if r in ("q", "quit", "exit"):
            return Mode.QUIT
        if r in ("", "1"):
            return Mode.ALBUM
        if r == "2":
            return Mode.ARTIST
        if r == "3":
            return Mode.WALK
        if r == "4":
            return Mode.WALK_QUEUE
        if r == "5":
            return Mode.ALBUM_WALK
        if r == "6":
            return Mode.ALBUM_REPAIR
        if r == "7":
            return Mode.UPGRADE
        log.info(fmt(C.GRAY, "  Enter 1-7 (blank = 1) or q to quit."))
