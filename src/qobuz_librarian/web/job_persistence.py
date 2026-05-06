"""Write-through SQLite persistence for the job registry.

Without this, the registry + work queues in ``jobs.py`` are in-memory only:
a container restart (compose update, OOM, host reboot) silently drops every
queued/running download (orphaning staging) and throws away a completed
scan's AWAITING_REVIEW candidates — minutes of API work gone, the user
re-scans from artist #1.

With this module:

* Job state is mirrored into ``DATA_DIR/jobs.db`` on every meaningful
  transition (add, RUNNING start, AWAITING_REVIEW, approve, terminal).
* On startup, ``restore`` reloads the rows into the registry:
  - DONE / FAILED / CANCELED come back as historical entries on /queue.
  - AWAITING_REVIEW comes back with candidates intact so the user can
    still approve. The execute function is rebound from a kind registry
    the caller passes in — closures themselves aren't serialisable.
  - PENDING / RUNNING / SCANNING from the prior session are marked
    FAILED("interrupted on restart — submit again") so the user sees
    them rather than them silently vanishing.

Log lines and live progress are NOT persisted: too chatty (a long walk
logs thousands of lines) and not load-bearing for resume. A reloaded
job's log starts empty with one explanatory line.

If the SQLite file can't be opened (read-only volume, disk full), every
helper here degrades to a no-op — the in-memory registry still works,
the user just loses crash durability.
"""
import json
import logging
import sqlite3
import threading
from typing import Optional

from qobuz_librarian import config as cfg

_log = logging.getLogger("qobuz_librarian")
_lock = threading.Lock()
_disabled = False
_conn: Optional[sqlite3.Connection] = None
# The db opened fine but a write later failed (typically a full disk). Surface
# it once at INFO — durability is silently gone otherwise — then stay quiet so
# a stuck volume doesn't flood the log on every status change.
_warned_write_failure = False


def _note_write_failure(what: str, e: Exception) -> None:
    global _warned_write_failure
    if not _warned_write_failure:
        _warned_write_failure = True
        _log.info("job persistence write failed (%s); jobs may not survive a "
                  "restart until the volume recovers: %s", what, e)
    else:
        _log.debug("%s failed: %s", what, e)


def _path():
    return cfg.DATA_DIR / "jobs.db"


def _get_conn() -> Optional[sqlite3.Connection]:
    """Return the persistent WAL connection, opening it on first call.

    Returns None and disables further attempts when the volume isn't
    writable — the in-memory registry is still correct, the user just
    forgoes restart durability rather than seeing a stream of OSError
    on every status change.
    """
    global _disabled, _conn
    if _disabled:
        return None
    if _conn is not None:
        return _conn
    try:
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_path()), timeout=5.0,
                                check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        return _conn
    except (OSError, sqlite3.Error) as e:
        _log.info("job persistence disabled (%s); jobs won't survive restart.", e)
        _disabled = True
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    artist        TEXT NOT NULL DEFAULT '',
    album_id      TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'download',
    status        TEXT NOT NULL,
    phase         TEXT NOT NULL DEFAULT '',
    candidates    TEXT NOT NULL DEFAULT '[]',
    error         TEXT,
    summary       TEXT NOT NULL DEFAULT '',
    review_verb   TEXT NOT NULL DEFAULT 'Download',
    execute_kind  TEXT NOT NULL DEFAULT '',
    execute_args  TEXT NOT NULL DEFAULT '{}',
    created_at    REAL,
    finished_at   REAL
)
"""


def init() -> None:
    """Create the schema. Safe to call repeatedly."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        conn.execute(_SCHEMA)
        conn.commit()


def persist(job) -> None:
    """Write the job's current state to disk. Idempotent (INSERT OR REPLACE)."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(id, title, artist, album_id, kind, status, phase, candidates, "
                " error, summary, review_verb, execute_kind, execute_args, "
                " created_at, finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.id, job.title or "", job.artist or "",
                    job.album_id or "", job.kind or "download",
                    job.status.value if hasattr(job.status, "value") else str(job.status),
                    job.phase or "",
                    json.dumps(job.candidates or []),
                    job.error,
                    job.summary or "",
                    job.review_verb or "Download",
                    job.execute_kind or "",
                    json.dumps(job.execute_args or {}),
                    job.created_at,
                    job.finished_at,
                ),
            )
            conn.commit()
        except sqlite3.Error as e:
            _note_write_failure(f"persist {job.id}", e)


def delete(job_id: str) -> None:
    """Drop the row for a job pruned from the registry."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            conn.commit()
        except sqlite3.Error:
            pass


def load_one(job_id: str) -> Optional[dict]:
    """Return one persisted job by id (the same shape ``load_all`` yields
    per row), or None if it isn't on disk. Used by the read-only "this
    job was archived" page so a registry eviction doesn't make a job's
    history disappear from view."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT id, title, artist, album_id, kind, status, phase, "
                "candidates, error, summary, review_verb, execute_kind, "
                "execute_args, created_at, finished_at FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        except sqlite3.Error as e:
            _log.debug("load_one failed for %s: %s", job_id, e)
            return None
    if row is None:
        return None
    try:
        return {
            "id": row[0], "title": row[1], "artist": row[2], "album_id": row[3],
            "kind": row[4], "status": row[5], "phase": row[6],
            "candidates": json.loads(row[7] or "[]"),
            "error": row[8], "summary": row[9] or "", "review_verb": row[10] or "Download",
            "execute_kind": row[11] or "",
            "execute_args": json.loads(row[12] or "{}"),
            "created_at": row[13], "finished_at": row[14],
        }
    except (ValueError, TypeError):
        return None


def prune_finished(keep: int) -> None:
    """Drop the oldest terminal rows past ``keep`` so the archive doesn't
    grow without bound. Non-terminal jobs are never pruned here — they're
    live state, not history. Best-effort: a sqlite error logs and bows out."""
    if keep <= 0:
        return
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(
                "DELETE FROM jobs WHERE id IN ("
                "  SELECT id FROM jobs "
                "  WHERE status IN ('done', 'failed', 'canceled') "
                "  ORDER BY COALESCE(finished_at, created_at) DESC "
                "  LIMIT -1 OFFSET ?)",
                (keep,),
            )
            conn.commit()
        except sqlite3.Error as e:
            _log.debug("prune_finished(%d) failed: %s", keep, e)


def load_all() -> list[dict]:
    """Return every persisted job as a plain dict — caller rehydrates into
    a Job. Returns [] when the db can't be opened."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT id, title, artist, album_id, kind, status, phase, "
                "candidates, error, summary, review_verb, execute_kind, "
                "execute_args, created_at, finished_at FROM jobs "
                "ORDER BY created_at"
            ).fetchall()
        except sqlite3.Error as e:
            _log.info("couldn't read jobs.db on startup (%s); starting fresh.", e)
            return []
    out = []
    for r in rows:
        try:
            out.append({
                "id": r[0], "title": r[1], "artist": r[2], "album_id": r[3],
                "kind": r[4], "status": r[5], "phase": r[6],
                "candidates": json.loads(r[7] or "[]"),
                "error": r[8], "summary": r[9] or "", "review_verb": r[10] or "Download",
                "execute_kind": r[11] or "",
                "execute_args": json.loads(r[12] or "{}"),
                "created_at": r[13], "finished_at": r[14],
            })
        except (ValueError, TypeError) as e:
            _log.info("skipping unreadable jobs.db row %s: %s", r[0], e)
    return out


def _reset_for_tests() -> None:
    """Test-only hook: drop the on-disk db so a fresh test starts clean."""
    global _disabled, _conn, _warned_write_failure
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
    _disabled = False
    _warned_write_failure = False
    p = _path()
    for q in (p, p.with_suffix(".db-wal"), p.with_suffix(".db-shm")):
        try:
            q.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
