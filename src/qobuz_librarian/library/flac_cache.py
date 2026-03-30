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
import sqlite3
import threading

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli.logging import vlog

_init_lock = threading.Lock()
_initialized = False


def _db_path():
    from pathlib import Path
    return Path(str(cfg.DATA_DIR)) / "flac_cache.db"


def _connect():
    return sqlite3.connect(str(_db_path()), timeout=5)


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
            with _connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS files "
                    "(path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, "
                    "payload TEXT NOT NULL)")
                conn.commit()
            _initialized = True
            return True
        except sqlite3.Error as e:
            vlog(f"flac cache init failed ({e}); proceeding without it")
            return False


def _stat(path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def get(path) -> dict | None:
    """Cached tags for ``path`` if the file is unchanged since they were stored."""
    if not _ensure():
        return None
    sig = _stat(path)
    if sig is None:
        return None
    mtime_ns, size = sig
    try:
        with _connect() as conn:
            row = conn.execute(
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


def put(path, payload) -> None:
    if not isinstance(payload, dict) or not _ensure():
        return
    sig = _stat(path)
    if sig is None:
        return
    mtime_ns, size = sig
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO files (path, mtime_ns, size, payload) "
                "VALUES (?, ?, ?, ?)", (str(path), mtime_ns, size, data))
            conn.commit()
    except sqlite3.Error as e:
        vlog(f"flac cache write failed: {e}")


def _reset_for_tests() -> None:
    global _initialized
    _initialized = False
