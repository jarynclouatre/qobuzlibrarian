"""Persistent cache of ``get_album`` responses, keyed on album id.

A Qobuz album's metadata and track list don't change, so a fetched album is safe
to keep indefinitely. Library scans call ``get_album`` once per owned album to
materialise its track list (``get_artist_albums`` returns counts, not items), and
that per-album call is the dominant API cost of a scan. Serving it from a local
SQLite lookup turns a re-scan of an unchanged library from minutes of round-trips
into milliseconds.

SQLite (not one big JSON) so a few thousand albums are written incrementally and
read by id without rewriting the whole file, and so the web scan's parallel
workers can each open their own connection. Set ``ALBUM_CACHE_ENABLED=false`` to
turn it off; delete the db file to force a full refresh.
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


def _db_path() -> Path:
    return Path(str(cfg.DATA_DIR)) / "album_cache.db"


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(str(_db_path()), timeout=5)


def _ensure() -> bool:
    """Create the table once. Returns False if the cache can't be used."""
    global _initialized
    if not cfg.ALBUM_CACHE_ENABLED:
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
                    "CREATE TABLE IF NOT EXISTS albums "
                    "(id TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL)")
                # Artist catalogs change when new releases drop, so unlike album
                # track lists they're served with a TTL (see get_catalog).
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS catalogs "
                    "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL)")
                conn.commit()
            _initialized = True
            return True
        except sqlite3.Error as e:
            vlog(f"album cache init failed ({e}); proceeding without it")
            return False


def get(album_id) -> dict | None:
    if not album_id or not _ensure():
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT payload FROM albums WHERE id = ?", (str(album_id),)
            ).fetchone()
    except sqlite3.Error as e:
        vlog(f"album cache read failed: {e}")
        return None
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def put(album_id, payload) -> None:
    if not album_id or not isinstance(payload, dict) or not _ensure():
        return
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO albums (id, payload, fetched_at) "
                "VALUES (?, ?, ?)", (str(album_id), data, time.time()))
            conn.commit()
    except sqlite3.Error as e:
        vlog(f"album cache write failed: {e}")


def get_catalog(key, ttl_seconds) -> dict | None:
    """Cached artist-catalog payload for ``key`` if newer than ``ttl_seconds``.

    A non-positive TTL always misses, which disables catalog caching while
    leaving the (immutable) album cache on."""
    if not key or ttl_seconds <= 0 or not _ensure():
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at FROM catalogs WHERE key = ?",
                (str(key),)).fetchone()
    except sqlite3.Error as e:
        vlog(f"catalog cache read failed: {e}")
        return None
    if not row or (time.time() - (row[1] or 0)) > ttl_seconds:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def put_catalog(key, payload) -> None:
    if not key or not isinstance(payload, dict) or not _ensure():
        return
    try:
        data = json.dumps(payload)
    except (TypeError, ValueError):
        return
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO catalogs (key, payload, fetched_at) "
                "VALUES (?, ?, ?)", (str(key), data, time.time()))
            conn.commit()
    except sqlite3.Error as e:
        vlog(f"catalog cache write failed: {e}")


def _reset_for_tests() -> None:
    global _initialized
    _initialized = False
