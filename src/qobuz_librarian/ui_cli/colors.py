"""ANSI colour helpers."""
import os
import shutil
import sys


class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    BLUE    = "\033[94m"
    CYAN    = "\033[96m"
    GRAY    = "\033[90m"
    WHITE   = "\033[97m"
    MAGENTA = "\033[95m"


def _detect_color() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    _fc = os.environ.get("FORCE_COLOR", "")
    if _fc and _fc != "0":
        return True
    return sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"


_enabled = _detect_color()


def set_color_enabled(enabled: bool):
    global _enabled
    _enabled = enabled


def fmt(color, text):
    if not _enabled:
        return str(text)
    return f"{color}{text}{C.RESET}"


def term_width(default=80):
    try:
        w = shutil.get_terminal_size((default, 24)).columns
        return max(40, w)
    except OSError:
        return default


def truncate(s, n):
    s = str(s)
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)].rstrip() + "…"


def banner(title, color=None):
    # Goes through the shared logger so the same call site renders to
    # both the CLI stdout AND the web UI's captured SSE stream — using
    # bare print() here loses the banner in the web log.
    from qobuz_librarian.ui_cli.logging import log
    color = color or C.BLUE
    # Cap at 100: enough rule for a wide desktop terminal without it
    # looking ridiculous on an ultrawide; still fits a narrow ~60-col
    # window because term_width() never returns below 40.
    w = min(term_width(), 100)
    log.info("")
    log.info(fmt(C.BOLD + color, "═" * w))
    log.info(f"  {fmt(C.BOLD + C.WHITE, title)}")
    log.info(fmt(C.BOLD + color, "═" * w))


def section(title, color=None):
    from qobuz_librarian.ui_cli.logging import log
    color = color or C.BLUE
    log.info("")
    log.info(f"  {fmt(C.BOLD + color, '── ' + title + ' ──')}")


def format_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
