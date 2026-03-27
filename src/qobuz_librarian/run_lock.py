"""Single-instance run lock shared by the CLI and web worker.

Two writers into /staging at the same time corrupts both their downloads,
so only one Qobuz Librarian "run" (CLI invocation or web worker process)
holds the lock at any time. Uses fcntl.flock — kernel releases the lock
on process exit (including SIGKILL), so there's no stale-lock cleanup.

The lock file lives in DATA_DIR (a shared volume in Docker) so a
``docker compose run --rm qobuz-librarian cli ...`` from a separate
container is blocked while the web container holds the lock.
"""
import fcntl
import os
from typing import Optional, TextIO

from qobuz_librarian import config as cfg


class LockBusy(Exception):
    """Raised by acquire() when another holder has the lock.

    The other-process PID is on ``self.pid`` when readable, else "?".
    """

    def __init__(self, pid: str = "?"):
        super().__init__(f"another run is active (pid {pid})")
        self.pid = pid


def acquire() -> Optional[TextIO]:
    """Acquire the run lock and return the file handle.

    Caller must keep a reference to the returned handle for the lock to
    hold; closing/garbage-collecting it releases the lock. Returns None
    if the lock file can't be opened (best-effort, never blocks the run).

    Raises LockBusy if another process already holds the lock.
    """
    try:
        cfg.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        # "a+" preserves existing content until we successfully acquire
        # the lock — "w" would truncate the file before flock could even
        # check, wiping the previous holder's PID.
        fp = open(cfg.LOCK_FILE, "a+", encoding="utf-8")
    except OSError:
        return None
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            fp.seek(0)
            other = fp.read().strip() or "?"
        except OSError:
            other = "?"
        fp.close()
        raise LockBusy(other)
    try:
        fp.seek(0)
        fp.truncate()
        fp.write(str(os.getpid()))
        fp.flush()
        # flush() pushes Python's buffer to the kernel; fsync() pushes the
        # kernel page cache to the disk. Without fsync, a hard crash within
        # seconds of acquiring the lock can leave the file empty or with a
        # stale PID — the next launch then reports "(pid ?)" in LockBusy.
        os.fsync(fp.fileno())
    except OSError as e:
        # The lock itself is held (flock succeeded above); only the PID write
        # failed, so a concurrent LockBusy may show "(pid ?)". Leave a trace.
        from qobuz_librarian.ui_cli.logging import vlog
        vlog(f"run-lock: couldn't record PID ({e}); lock is held regardless")
    return fp
