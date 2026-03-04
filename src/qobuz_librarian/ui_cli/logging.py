"""Logging setup and verbose-output helper.

All modules that need log or vlog import from here. _VERBOSE is a module-level
flag; set_verbose() is called by cli.py at startup based on --verbose arg.
"""
import logging
import re
import sys

from qobuz_librarian.ui_cli.colors import C, fmt

log = logging.getLogger("qobuz_librarian")
log.setLevel(logging.INFO)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(message)s"))
if not log.handlers:
    log.addHandler(_sh)

# ANSI escape codes pollute the file log; strip before writing.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _StripAnsiFormatter(logging.Formatter):
    def format(self, record):
        s = super().format(record)
        return _ANSI_RE.sub("", s)


_file_handler = None


def attach_file_handler(path, level_name: str = "INFO"):
    """Attach a rotating file handler at `path`. Idempotent — safe to call
    from both _entry() and the web _lifespan."""
    global _file_handler
    if _file_handler is not None:
        return
    from logging.handlers import RotatingFileHandler
    from pathlib import Path
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(p, maxBytes=5 * 1024 * 1024, backupCount=3,
                                encoding="utf-8")
        h.setFormatter(_StripAnsiFormatter(
            "%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        level = getattr(logging, level_name.upper(), logging.INFO)
        h.setLevel(level)
        log.addHandler(h)
        _file_handler = h
    except OSError:
        # Logging is best-effort — don't crash startup if the data volume
        # isn't writable.
        pass

# Progress-reporting hook. Like rip.py's cancel-check, the web layer injects a
# reporter that routes structured progress (phase + counts) to the running
# job's live header. Outside the web (CLI) it stays a no-op, so download/scan
# code can call report_progress() unconditionally without knowing about jobs.
_progress_reporter = None


def set_progress_reporter(fn):
    global _progress_reporter
    _progress_reporter = fn


def report_progress(phase, current=0, total=0, item=""):
    if _progress_reporter is not None:
        try:
            _progress_reporter(phase, current, total, item)
        except Exception:
            pass


_VERBOSE = False


def set_verbose(v: bool):
    global _VERBOSE
    _VERBOSE = v


def set_quiet(quiet: bool):
    """Raise the logger threshold so log.info calls are suppressed.
    Errors and warnings still pass."""
    log.setLevel(logging.WARNING if quiet else logging.INFO)


def vlog(msg):
    if _VERBOSE:
        log.info(fmt(C.GRAY, f"    {msg}"))
