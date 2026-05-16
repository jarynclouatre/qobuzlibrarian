"""Persistent cache of parsed FLAC tags, keyed on path + mtime + size.

Every library scan re-parses every audio file with mutagen; on a large library
that's tens of thousands of reads redone each run even when nothing changed.
Caching the parsed tags against the file's mtime (nanoseconds) and size means an
unchanged file costs one ``stat()`` and a SQLite lookup instead of a full parse.
A file edited or replaced changes its mtime/size, so the cache self-invalidates
— no stale tags. Set ``FLAC_CACHE_ENABLED=false`` to disable; delete the db to
force a re-parse.
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli.logging import vlog

_init_lock = threading.Lock()
_initialized = False
_local = threading.local()


def _db_path():
    return Path(str(cfg.DATA_DIR)) / "flac_cache.db"


def _ensure() -> bool:
    global _initialized
    if not cfg.FLAC_CACHE_ENABLED:
        return False
    if _initialized:
        return True
    with _init_lock:
        if _initialized:
            return True
        try:
            _db_path().parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(_db_path()), timeout=5)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS files "
                    "(path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, "
                    "payload TEXT NOT NULL)")
                conn.commit()
            finally:
                conn.close()
            _initialized = True
            return True
        except sqlite3.Error as e:
            vlog(f"flac cache init failed ({e}); proceeding without it")
            return False


def _conn():
    """Connection scoped to the calling thread.

    A scan reads tens of thousands of files; opening a fresh connection per
    lookup costs ~20x the lookup it's meant to make cheap, so each thread
    keeps one (SQLite connections can't be shared across threads). synchronous
    is dropped to NORMAL — this is a self-invalidating cache, so a row lost to
    a crash is just re-parsed next scan, and the per-write fsync it avoids is
    otherwise the bulk of a cold scan's caching cost.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(_db_path()), timeout=5)
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def signature(path):
    """``(mtime_ns, size)`` for ``path``, or None if it can't be stat'd — the
    key that detects a file changing out from under a stored entry. Capture it
    BEFORE reading a file you intend to ``put()``, so an edit landing between the
    read and the store doesn't pair the file's new mtime with the old tags."""
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def get(path) -> dict | None:
    """Cached tags for ``path`` if the file is unchanged since they were stored."""
    if not _ensure():
        return None
    sig = signature(path)
    if sig is None:
        return None
    mtime_ns, size = sig
    try:
        row = _conn().execute(
            "SELECT mtime_ns, size, payload FROM files WHERE path = ?",
            (str(path),)).fetchone()
    except sqlite3.Error as e:
        vlog(f"flac cache read failed: {e}")
        return None
    if not row or row[0] != mtime_ns or row[1] != size:
        return None
    try:
        return json.loads(row[2])
    except (ValueError, TypeError):
        return None


def put(path, payload, sig=None) -> None:
    """Store parsed tags for ``path``. Pass ``sig`` from ``signature(path)``
    captured before the file was read; otherwise a file edited during the parse
    is recorded with its new mtime but the pre-edit tags and served stale until
    it changes again. Falls back to statting now when the caller omits it."""
    if not isinstance(payload, dict) or not _ensure():
        return
    if sig is None:
        sig = signature(path)
    if sig is None:
        return
    mtime_ns, size = sig
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        conn = _conn()
        conn.execute(
            "INSERT OR REPLACE INTO files (path, mtime_ns, size, payload) "
            "VALUES (?, ?, ?, ?)", (str(path), mtime_ns, size, data))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"flac cache write failed: {e}")


def prune_missing(force: bool = False) -> int:
    """Drop rows whose file is gone, keeping the db proportional to the library.

    Keying on absolute path means every upgrade-replace, move, or consolidation
    leaves the old path's row orphaned, so the table would otherwise grow
    without bound. Throttled to once a day — a CLI session that opens and closes
    repeatedly shouldn't re-walk the whole table each time — and skipped when
    MUSIC_ROOT is absent so an unmounted library volume can't wipe the cache.
    """
    if not _ensure() or not cfg.MUSIC_ROOT.exists():
        return 0
    stamp = Path(str(cfg.DATA_DIR)) / ".flac_cache_prune"
    if not force and stamp.exists():
        try:
            if (time.time() - stamp.stat().st_mtime) < 86400:
                return 0
        except OSError:
            pass
    try:
        conn = _conn()
        gone = [(p,) for (p,) in conn.execute("SELECT path FROM files")
                if not os.path.exists(p)]
        if gone:
            conn.executemany("DELETE FROM files WHERE path = ?", gone)
            conn.commit()
    except sqlite3.Error as e:
        vlog(f"flac cache prune failed: {e}")
        return 0
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass
    return len(gone)


def _reset_for_tests() -> None:
    global _initialized
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
    _initialized = False
