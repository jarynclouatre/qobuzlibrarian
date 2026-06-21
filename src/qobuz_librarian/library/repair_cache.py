"""Per-album repair-scan result cache, keyed on the album folder's audio-file
signature (each audio file's name + size + mtime).

A repair scan is the slowest thing the tool does: it decode-tests every FLAC
*and* looks every track up on Qobuz. Without a cache, a re-scan redoes all of
that even when nothing changed. Caching each album's scan outcome against a
signature of its audio files means an unchanged album costs one directory
listing + a SQLite lookup instead of a full re-scan, so a re-scan only re-checks
albums that actually changed.

A stored result is trusted only while BOTH hold:

* the album's audio files are byte-for-byte the same (a repaired/edited/added
  file changes the signature, forcing a re-scan of just that album), and
* it is younger than ``REPAIR_CACHE_TTL_DAYS`` — so even an untouched album
  re-verifies against Qobuz at least that often, in case a track's catalogue
  entry changed.

The one gap (shared with ``flac_cache``) is an in-place edit that preserves both
size and mtime, which keeps the cached result until the file next changes. Set
``REPAIR_CACHE_ENABLED=false`` to disable; delete the db to force a full re-scan.
"""
import json
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
    return Path(str(cfg.DATA_DIR)) / "repair_cache.db"


def _is_corrupt_error(e: sqlite3.Error) -> bool:
    msg = str(e).lower()
    return any(s in msg for s in
               ("malformed", "not a database", "file is encrypted"))


def _discard_corrupt_db() -> bool:
    """Delete a malformed cache db (+ WAL sidecars). The cache is derived data —
    losing it just makes the next scan recompute, which beats a permanently dead
    cache. Returns True if anything was cleared."""
    db = _db_path()
    cleared = False
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        try:
            p.unlink()
            cleared = True
        except FileNotFoundError:
            pass
        except OSError as e:
            vlog(f"couldn't clear corrupt repair cache {p.name}: {e}")
            return False
    return cleared


def _handle_db_error(e: sqlite3.Error) -> None:
    """Drop this thread's connection and, on a corrupt-db error, discard the
    malformed file so the next _ensure() rebuilds — same recovery flac_cache
    uses."""
    global _initialized
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None
    if not _is_corrupt_error(e):
        return
    with _init_lock:
        if _initialized and _discard_corrupt_db():
            _initialized = False
            import logging
            logging.getLogger("qobuz_librarian").info(
                "repair cache was corrupt — discarded; it rebuilds on next scan")


def _ensure() -> bool:
    global _initialized
    if not cfg.REPAIR_CACHE_ENABLED:
        return False
    if _initialized:
        return True
    with _init_lock:
        if _initialized:
            return True
        try:
            _db_path().parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            vlog(f"repair cache dir unavailable ({e}); proceeding without it")
            return False
        for attempt in (1, 2):
            try:
                conn = sqlite3.connect(str(_db_path()), timeout=5)
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS albums "
                        "(path TEXT PRIMARY KEY, sig TEXT NOT NULL, "
                        "stored_at INTEGER NOT NULL, payload TEXT NOT NULL)")
                    conn.commit()
                finally:
                    conn.close()
                _initialized = True
                return True
            except sqlite3.Error as e:
                if attempt == 1 and _is_corrupt_error(e) and _discard_corrupt_db():
                    continue
                vlog(f"repair cache init failed ({e}); proceeding without it")
                return False
        return False


def _conn():
    """Connection scoped to the calling thread (SQLite connections can't be
    shared across the scan's worker threads). synchronous=NORMAL — a row lost to
    a crash just re-scans that album next run."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(_db_path()), timeout=5)
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def signature(album_dir) -> str | None:
    """A stable string over the album's audio files — sorted ``name|size|mtime_ns``
    — that changes whenever any audio file is added, removed, replaced, or
    re-encoded. Returns None if the folder can't be listed or holds no audio
    (nothing to key on, so the caller scans without caching)."""
    try:
        exts = cfg.AUDIO_EXTS
        parts = []
        for p in sorted(Path(album_dir).iterdir()):
            if p.suffix.lower() in exts:
                try:
                    st = p.stat()
                except OSError:
                    return None
                parts.append(f"{p.name}|{st.st_size}|{st.st_mtime_ns}")
        return "\n".join(parts) if parts else None
    except (OSError, TypeError, ValueError):
        return None


def get(album_dir, sig) -> dict | None:
    """The cached scan outcome for this album if its files are unchanged
    (signature matches) and the entry is within REPAIR_CACHE_TTL_DAYS."""
    if sig is None or not _ensure():
        return None
    try:
        row = _conn().execute(
            "SELECT sig, stored_at, payload FROM albums WHERE path = ?",
            (str(album_dir),)).fetchone()
    except sqlite3.Error as e:
        vlog(f"repair cache read failed: {e}")
        _handle_db_error(e)
        return None
    if not row or row[0] != sig:
        return None
    ttl = float(cfg.REPAIR_CACHE_TTL_DAYS) * 86400
    if ttl > 0 and (time.time() - row[1]) > ttl:
        return None
    try:
        return json.loads(row[2])
    except (ValueError, TypeError):
        return None


def put(album_dir, sig, payload) -> None:
    """Store an album's scan outcome under its current signature. Pass the ``sig``
    captured BEFORE the scan so a file edited mid-scan isn't paired with the
    pre-edit result and served stale until it changes again."""
    if sig is None or not isinstance(payload, dict) or not _ensure():
        return
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        conn = _conn()
        conn.execute(
            "INSERT OR REPLACE INTO albums (path, sig, stored_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (str(album_dir), sig, int(time.time()), data))
        conn.commit()
    except sqlite3.Error as e:
        vlog(f"repair cache write failed: {e}")
        _handle_db_error(e)
