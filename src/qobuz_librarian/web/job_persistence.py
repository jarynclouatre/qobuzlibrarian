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
  - DONE / FAILED / CANCELED come back as historical entries browsable
    in the History view (see ``history_page`` / ``history_count``).
  - AWAITING_REVIEW comes back with candidates intact so the user can
    still approve. The execute function is resolved from ``execute_kind``
    via a lookup table the caller provides — closures aren't serialisable.
  - PENDING / RUNNING from the prior session are marked
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
    finished_at   REAL,
    single        TEXT NOT NULL DEFAULT '{}'
)
"""


_SCHEMA_VERSION = 2


def init() -> None:
    """Create the schema (and run additive migrations). Safe to call repeatedly."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(_SCHEMA)
            # Terminal-row index: history_count() / history_page() / prune_finished()
            # all filter on status and order by finished_at. Without this they full-
            # scan the table, touching every row's record header just to skip past
            # the multi-MB ``candidates`` blob of parked reviews. COUNT(*) is served
            # entirely from the index; the page query uses it to filter+sort before
            # fetching only the LIMIT rows. created_at is the ORDER BY's COALESCE
            # fallback, so it's carried in the index too. IF NOT EXISTS = idempotent.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_terminal "
                "ON jobs(status, finished_at, created_at)"
            )
            # Schema versioning so a FUTURE column addition can ALTER TABLE instead
            # of silently failing every persist() against an old jobs.db — that
            # failure is swallowed by _note_write_failure, leaving the archive
            # non-durable with no visible sign. To add a column later: bump
            # _SCHEMA_VERSION and add an `if version < N: conn.execute("ALTER TABLE
            # jobs ADD COLUMN ...")` block here (SQLite ADD COLUMN is online-safe).
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < _SCHEMA_VERSION:
                # v2: persist Job.single (single-track-grab undo info) so a restart
                # doesn't drop the Undo affordance on a completed one-track grab.
                # _SCHEMA already adds the column for a fresh db (CREATE TABLE), so
                # only ALTER an existing table that predates it. ADD COLUMN is
                # online-safe and the DEFAULT backfills old rows with '{}'.
                cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
                if "single" not in cols:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN single TEXT NOT NULL DEFAULT '{}'")
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.commit()
        except sqlite3.Error as e:
            # A transient/locked/full/corrupt jobs.db here would otherwise
            # propagate out of restore_jobs() into the caller's broad "couldn't
            # restore prior jobs — starting fresh" handler, masking a recoverable
            # condition and then leaving every later persist() silently non-
            # durable. Surface it distinctly and degrade to the in-memory
            # registry; a restart once the volume recovers re-runs this.
            _log.warning("job persistence schema/migration failed; running "
                         "without crash durability until the volume recovers "
                         "and the app restarts: %s", e)


def persist(job) -> None:
    """Write the job's current state to disk. Idempotent (INSERT OR REPLACE)."""
    # Serialize the payloads before taking the lock — a parked review can hold
    # hundreds of candidate dicts, and json.dumps of that shouldn't run while
    # the single persistence lock is held, stalling a concurrent history read or
    # the other worker lane's write. default=str so one stray non-JSON value (a
    # Path that slipped into a payload) coerces to text instead of raising
    # TypeError, which would escape the sqlite3.Error guard, crash the worker,
    # and silently drop a parked review the user can't get back.
    # Snapshot the candidate list under the job's own lock first: set_selected /
    # set_all_selected mutate it under that lock, so dumping it unlocked could
    # serialize a torn selection or trip over a concurrent resize.
    with job._lock:
        candidates_snapshot = list(job.candidates or [])
    candidates_json = json.dumps(candidates_snapshot, default=str)
    execute_args_json = json.dumps(job.execute_args or {}, default=str)
    single_json = json.dumps(job.single or {}, default=str)
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(id, title, artist, album_id, kind, status, phase, candidates, "
                " error, summary, review_verb, execute_kind, execute_args, "
                " created_at, finished_at, single) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.id, job.title or "", job.artist or "",
                    job.album_id or "", job.kind or "download",
                    job.status.value if hasattr(job.status, "value") else str(job.status),
                    job.phase or "",
                    candidates_json,
                    job.error,
                    job.summary or "",
                    job.review_verb or "Download",
                    job.execute_kind or "",
                    execute_args_json,
                    job.created_at,
                    job.finished_at,
                    single_json,
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
                "execute_args, created_at, finished_at, single FROM jobs WHERE id=?",
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
            "single": json.loads(row[15] or "{}"),
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


_TERMINAL_SQL = "status IN ('done', 'failed', 'canceled')"


def history_count() -> int:
    """How many finished jobs are on disk — for paginating the History view."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return 0
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {_TERMINAL_SQL}").fetchone()[0]
        except sqlite3.Error:
            return 0


def history_page(limit: int, offset: int) -> list[dict]:
    """A page of finished jobs, newest first — the browsable record behind the
    History view. Lighter than ``load_all`` (no candidates/args): just what a
    history row shows, plus the id to open the full job. The ``id`` tiebreaker
    keeps paging stable when finish times collide."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT id, title, artist, album_id, status, error, summary, "
                "execute_kind, created_at, finished_at FROM jobs "
                f"WHERE {_TERMINAL_SQL} "
                "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        except sqlite3.Error as e:
            _log.debug("history_page failed: %s", e)
            return []
    return [{
        "id": r[0], "title": r[1] or "", "artist": r[2] or "",
        "album_id": r[3] or "", "status": r[4], "error": r[5],
        "summary": r[6] or "", "execute_kind": r[7] or "",
        "created_at": r[8], "finished_at": r[9],
    } for r in rows]


def last_finished_at(execute_kind: str) -> Optional[float]:
    """When a job of this ``execute_kind`` last finished cleanly, or None — backs
    the per-tool "Last scan …" freshness line. Only DONE counts (a failed/cancelled
    run isn't a completed pass), and the archive outlives the in-memory cap, so the
    line survives restarts. Reads the durable jobs.db rather than a separate stamp
    file so there's nothing extra to keep in sync."""
    if not execute_kind:
        return None
    with _lock:
        conn = _get_conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT MAX(finished_at) FROM jobs "
                "WHERE status='done' AND execute_kind=? AND finished_at IS NOT NULL",
                (execute_kind,),
            ).fetchone()
        except sqlite3.Error:
            return None
    return row[0] if row and row[0] is not None else None


def clear_history() -> None:
    """Delete every finished job from disk — the user clearing the History
    record. In-flight jobs are untouched."""
    with _lock:
        conn = _get_conn()
        if conn is None:
            return
        try:
            conn.execute(f"DELETE FROM jobs WHERE {_TERMINAL_SQL}")
            conn.commit()
        except sqlite3.Error as e:
            _log.debug("clear_history failed: %s", e)


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
                "execute_args, created_at, finished_at, single FROM jobs "
                "ORDER BY created_at"
            ).fetchall()
        except sqlite3.Error as e:
            _log.info("couldn't read jobs.db on startup (%s); starting fresh.", e)
            return []
    out = []
    for r in rows:
        try:
            # Only AWAITING_REVIEW jobs need their candidates on restore; all
            # other statuses are either rehydrated as live state (RUNNING →
            # FAILED) or displayed as history without candidates. Skipping the
            # json.loads for the rest avoids deserialising ~950 multi-MB blobs
            # on startup when only a handful of parked reviews exist.
            candidates = json.loads(r[7] or "[]") if r[5] == "awaiting_review" else []
            out.append({
                "id": r[0], "title": r[1], "artist": r[2], "album_id": r[3],
                "kind": r[4], "status": r[5], "phase": r[6],
                "candidates": candidates,
                "error": r[8], "summary": r[9] or "", "review_verb": r[10] or "Download",
                "execute_kind": r[11] or "",
                "execute_args": json.loads(r[12] or "{}"),
                "created_at": r[13], "finished_at": r[14],
                "single": json.loads(r[15] or "{}"),
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
