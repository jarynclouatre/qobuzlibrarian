"""Interactive session mode picker."""
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.ui_cli.sentinels import Mode


def interactive_session_mode():
    """Top-of-loop menu. Re-prompts on unrecognized input rather than
    falling through to album mode on a typo."""
    print()
    log.info(fmt(C.GRAY, "  Qobuz Librarian: download Qobuz albums and fill your library's gaps."))
    log.info(fmt(C.BOLD + C.CYAN, "  What would you like to do?"))
    log.info(fmt(C.WHITE, "    1) Search       — find and download one album (name or Qobuz URL)"))
    log.info(fmt(C.WHITE, "    2) Artist       — one artist: fill gaps, then offer albums you're missing"))
    log.info(fmt(C.WHITE, "    3) Library walk — every artist, same as Artist; queue as you go and"))
    log.info(fmt(C.GRAY,  "                      download after each artist or all at the end"))
    log.info(fmt(C.WHITE, "    4) Album gaps   — every incomplete album you own: fill missing tracks"))
    log.info(fmt(C.GRAY,  "                      only, never suggests albums you don't have"))
    log.info(fmt(C.WHITE, "    5) Repair       — re-download corrupt/truncated tracks you own"))
    log.info(fmt(C.WHITE, "    6) Upgrade      — better-quality versions of albums you own"))
    log.info(fmt(C.WHITE, "    7) Migrate      — one-time setup: reorganize an existing library into the"))
    log.info(fmt(C.GRAY,  "                      Artist/Album layout (copies by default, never touches originals)"))
    log.info(fmt(C.WHITE, "    8) Downsample   — shrink hi-res files you own down to CD quality to reclaim space"))
    log.info(fmt(C.WHITE, "    9) Lyrics       — fetch lyrics for tracks you own that are missing them"))
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
            return Mode.WALK_QUEUE
        if r == "4":
            return Mode.ALBUM_WALK
        if r == "5":
            return Mode.ALBUM_REPAIR
        if r == "6":
            return Mode.UPGRADE
        if r == "7":
            return Mode.MIGRATE
        if r == "8":
            return Mode.DOWNSAMPLE
        if r == "9":
            return Mode.LYRICS
        log.info(fmt(C.GRAY, "  Enter 1-9 (blank = 1) or q to quit."))
