"""Background job system for the web UI.

Two job shapes share one worker and one log-streaming mechanism:

* **Simple job** — `submit(job, fn)`. Runs `fn(job)` to completion. Used for a
  single-album download.
* **Scan / review / execute job** — `submit_scan(job, scan_fn, execute_fn)`.
  `scan_fn(job)` inspects the library/catalog and attaches *candidates*. The
  job then parks in `AWAITING_REVIEW` so the user can pick which candidates to
  act on in the web UI. `approve(job, ids)` resumes it and runs
  `execute_fn(job, selected)` on the worker. This is the backbone of the
  artist / library / repair / upgrade flows, which are interactive by nature
  and can't just stream a terminal prompt to a browser.

Progress is captured from the shared ``qobuz_librarian`` logger and streamed
to connected SSE clients.
"""
import json
import logging
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from qobuz_librarian.integrations import rip as rip_module
from qobuz_librarian.ui_cli.logging import set_progress_reporter, set_thread_wrapper
from qobuz_librarian.web import job_persistence

# Thread-local pointer to the job currently being run on this worker.
# Lets rip_url's cancel-check hook (installed below) find the running
# job's cancel_requested flag without threading job through every layer.
_TLS = threading.local()


def _current_job_cancel_requested() -> bool:
    j = getattr(_TLS, "current_job", None)
    return bool(j and j.cancel_requested)


def _adopt_current_job(job):
    """ThreadPoolExecutor initializer: tag each pool worker with the job that
    spawned the pool so log records emitted from pool threads are attributed to
    the right job (not dropped by JobLogHandler's thread filter)."""
    _TLS.current_job = job


def pool_initializer_kwargs() -> dict:
    """kwargs for a ThreadPoolExecutor created INSIDE a job's worker thread, so
    its workers inherit this job via _TLS.current_job. Must be called on the
    worker thread (where current_job is set). Outside a job (CLI) it captures
    None, which is harmless (no JobLogHandler is attached there)."""
    return {"initializer": _adopt_current_job,
            "initargs": (getattr(_TLS, "current_job", None),)}


def _propagate_job_to_thread(target):
    """Wrap a helper-thread target so it inherits the SPAWNING thread's
    current_job. Used for rip.py's output-reader thread: it logs streamrip's
    live progress + per-track errors via the shared logger, and JobLogHandler
    routes records by thread — so without carrying the job onto that thread its
    lines would be dropped. current_job is captured here, on the spawning
    (worker) thread; the wrapped target re-applies it on the helper thread."""
    job = getattr(_TLS, "current_job", None)

    def _wrapped(*args, **kwargs):
        _TLS.current_job = job
        return target(*args, **kwargs)

    return _wrapped


rip_module.set_cancel_check(_current_job_cancel_requested)
# Carry the running job onto subprocess-reader helper threads (rip + beets) so
# their live-output log lines reach the right job instead of being dropped by
# JobLogHandler's per-thread routing. One hook at the shared logging module
# covers both readers.
set_thread_wrapper(_propagate_job_to_thread)


def _report_progress_to_current_job(phase, current, total, item):
    j = getattr(_TLS, "current_job", None)
    if j is not None:
        j.push_progress(phase, current, total, item)


set_progress_reporter(_report_progress_to_current_job)


class JobStatus(str, Enum):
    PENDING         = "pending"
    SCANNING        = "scanning"
    AWAITING_REVIEW = "awaiting_review"
    RUNNING         = "running"
    DONE            = "done"
    FAILED          = "failed"
    CANCELED        = "canceled"


TERMINAL = (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELED)
ACTIVE = (JobStatus.PENDING, JobStatus.SCANNING,
          JobStatus.AWAITING_REVIEW, JobStatus.RUNNING)

# Sentinel sent to live SSE subscribers to close the current phase's stream.
# It is intentionally NOT stored in log_lines: a scan/execute job streams in
# two phases, and a replay for a late subscriber must not contain a stale
# end-marker that would close the new phase's stream prematurely.
STREAM_END = "__DONE__"

# Sentinel prefix marking a fanned-out line as a structured progress update
# (phase + counts) rather than log output. push_line() strips NUL bytes, so a
# leading NUL can only come from push_progress — the SSE layer splits on it and
# emits a separate `event: progress` the page renders as a live header.
PROGRESS_PREFIX = "\x00PROGRESS\x00"

# Sentinel fanned out when a review's selection or candidate set changes, so
# other open tabs of the same review refresh live. Like PROGRESS_PREFIX it
# starts with a NUL push_line() can't produce, so the SSE layer can tell it
# apart from log output and emit a distinct `event: review`.
REVIEW_CHANGED = "\x00REVIEW\x00"


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Job:
    id: str           = field(default_factory=_new_id)
    title: str        = ""
    artist: str       = ""
    album_id: str     = ""
    # Single-track-grab undo info, set by the Get-track flow: the resolved album
    # dir, the grabbed track's isrc/number, and whether it marked the album a
    # single / created a new folder — enough for /undo to cleanly reverse it.
    # Empty for every other job; its presence is also how the UI knows to hide
    # Cancel (a one-track grab finishes before you could catch it) and show Undo.
    single: dict      = field(default_factory=dict)
    kind: str         = "download"          # download | scan
    status: JobStatus = JobStatus.PENDING
    phase: str        = ""                  # "", scan, execute
    # Live progress header (separate from the log): "Scanning 430/1882 · Beyoncé"
    progress_phase: str   = ""
    progress_current: int = 0
    progress_total: int   = 0
    progress_item: str    = ""
    progress_found: int   = 0   # running tally of results found so far
    log_lines: list   = field(default_factory=list)
    # Review candidates: each is a dict
    #   {cid, kind, title, artist, detail, payload, selected}
    candidates: list  = field(default_factory=list)
    error: Optional[str] = None
    # Short, user-facing outcome shown as a callout on the finished job page —
    # e.g. why a scan found nothing — so it isn't buried in the log.
    summary: str = ""
    # The verb the review screen's submit button uses. Most jobs download what
    # you approve; the migration job copies (or moves), so it overrides this.
    review_verb: str = "Download"
    # How to rebind execute_fn after a container restart. The closure passed
    # into submit_scan can't be serialised; the kind names a flow in the
    # resume registry (see web/app.py _RESUME_EXECUTE) and execute_args holds
    # any extra inputs that flow needs (migration's dest path, in_place flag).
    # Left as "" / {} for jobs that don't need to resume.
    execute_kind: str = ""
    execute_args: dict = field(default_factory=dict)
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)
    # When the job actually started working (left the queue). The elapsed clock
    # counts from here so it shows scan/download time, not how long it sat
    # pending; falls back to created_at while still queued or after a restart.
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    _execute_fn: Optional[Callable] = field(default=None, repr=False)
    _subscribers: list = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Monotonic candidate counter. cids are c{seq} and never reused, so a hide
    # that drops candidates mid-scan can't collide with a candidate the scan
    # appends afterwards (a length-based id would).
    _cand_seq: int = field(default=0, repr=False)
    # Set once a scan hits JOB_CANDIDATE_CAP and stops listing further finds.
    _candidate_cap_noted: bool = field(default=False, repr=False)

    # ── logging / streaming ──────────────────────────────────────────────────
    def _fan_out(self, line: str):
        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    # A consumer that fell behind (throttled tab, slow link):
                    # drop its oldest buffered line to keep the live tail — and
                    # the closing STREAM_END — flowing rather than freezing the
                    # stream. The complete log is always on the job page. Only
                    # _fan_out puts (under this lock) and only the reader gets,
                    # so the slot we free can't be taken before we reuse it.
                    try:
                        q.get_nowait()
                        q.put_nowait(line)
                    except (queue.Empty, queue.Full):
                        pass

    # Read from config at class-definition time so env overrides on
    # startup take effect (set JOB_LOG_CAP to lower for tight-memory NAS
    # boxes, or higher for long artist walks). Stays a class attribute so
    # tests that monkeypatch Job.LOG_CAP keep working unchanged.
    from qobuz_librarian import config as _cfg
    LOG_CAP = _cfg.JOB_LOG_CAP
    del _cfg

    _LOG_SLACK = 1000
    _TRUNCATION_MARKER = "[… earlier output truncated to bound memory …]"
    # Strip C0 control bytes except \t (\x09) and \n (\x0a) — a stray NUL or
    # ESC byte from streamrip/beets truncates some browsers' SSE display and
    # garbles the JSON status endpoint.
    _CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

    def push_line(self, line: str):
        """Append a real log line and stream it to live subscribers."""
        line = self._CTRL_RE.sub("", line)
        # Mutate log_lines under the lock: subscribe()/the status API read it
        # under the same lock from request threads, and the truncation `del`
        # below would otherwise tear a concurrent reader's slice.
        with self._lock:
            self.log_lines.append(line)
            if len(self.log_lines) > self.LOG_CAP + self._LOG_SLACK:
                del self.log_lines[:len(self.log_lines) - self.LOG_CAP]
                self.log_lines[0] = self._TRUNCATION_MARKER
        self._fan_out(line)

    def end_stream(self):
        """Close the current phase's live stream without storing a marker."""
        self._fan_out(STREAM_END)

    def push_progress(self, phase, current=0, total=0, item="", found=0,
                      hit=None):
        """Update the live progress header (phase + counts) and stream it as a
        distinct event. Kept out of log_lines so it never clutters the log or
        gets replayed line-by-line on reconnect; the latest snapshot is re-sent
        once when a new subscriber attaches.

        ``found`` is a running tally (e.g. albums with gaps so far); ``hit`` is
        a one-off {artist, albums} the scanning page appends to its live preview
        — it is deliberately left out of the stored snapshot so a reconnect
        replay doesn't duplicate a preview row."""
        # Write the snapshot fields under the lock so a reconnecting subscriber
        # reading them via _progress_snapshot() (also under the lock) can't catch
        # a torn mix — e.g. the new phase label with the previous phase's counts.
        with self._lock:
            self.progress_phase = phase
            self.progress_current = current
            self.progress_total = total
            self.progress_item = item
            self.progress_found = found
        payload = {"phase": phase, "current": current, "total": total,
                   "item": item, "found": found}
        if hit:
            payload["hit"] = hit
        self._fan_out(PROGRESS_PREFIX + json.dumps(payload))

    def notify_review_changed(self):
        """Tell every open review tab that selection/candidates changed, so a
        second tab (or phone) reflects a tick/untick/hide without a manual
        reload. Fanned out as a distinct event the SSE layer maps to
        `event: review`; carries nothing but a nudge — the tab re-fetches the
        authoritative counts itself."""
        self._fan_out(REVIEW_CHANGED)

    def _progress_snapshot(self) -> str:
        return PROGRESS_PREFIX + json.dumps({
            "phase": self.progress_phase, "current": self.progress_current,
            "total": self.progress_total, "item": self.progress_item,
            "found": self.progress_found})

    # Cap replay so a late subscriber doesn't get thousands of historical
    # lines blasted at them (and so the bounded queue isn't filled by
    # history alone — that would silently drop live lines). Default from
    # config.JOB_LOG_REPLAY_TAIL (env-tunable).
    from qobuz_librarian import config as _cfg2
    REPLAY_TAIL = _cfg2.JOB_LOG_REPLAY_TAIL
    del _cfg2

    def subscribe(self) -> "queue.Queue[str]":
        """Return a queue that replays the recent history then receives
        future lines. Only the last REPLAY_TAIL lines are replayed; for
        long-running jobs, full history is available on the job page from
        ``job.log_lines``.

        Snapshot + register is done under the lock so a push_line racing
        with subscribe doesn't drop a live line on the floor between the
        history snapshot and the subscriber appearing in the fan-out set.
        """
        q: queue.Queue[str] = queue.Queue(maxsize=2000)
        with self._lock:
            for line in self.log_lines[-self.REPLAY_TAIL:]:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    break
            # Re-send the current progress once so a reconnect/new tab shows the
            # live header immediately instead of a blank bar until the next tick.
            if self.progress_phase:
                try:
                    q.put_nowait(self._progress_snapshot())
                except queue.Full:
                    pass
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[str]"):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    # ── candidates ───────────────────────────────────────────────────────────
    from qobuz_librarian import config as _cfg3
    CANDIDATE_CAP = _cfg3.JOB_CANDIDATE_CAP
    del _cfg3

    def add_candidate(self, kind, title, artist="", detail="", payload=None,
                      selected=True):
        # Locked: a live scan appends from the worker thread while the request
        # thread may be reading/dropping candidates via the review screen.
        capped = False
        cid = None
        with self._lock:
            # Bound the in-memory (and persisted) candidate list so a runaway
            # whole-library scan can't exhaust memory. Past the cap, drop new
            # finds and flag it once so the user knows to narrow the scan.
            if len(self.candidates) >= self.CANDIDATE_CAP:
                capped = not self._candidate_cap_noted
                self._candidate_cap_noted = True
            else:
                # Append regardless of whether the cap was hit EARLIER: a
                # mid-scan hide can shrink the list back below the cap, and a
                # candidate we actually appended must return its real cid — the
                # sticky _candidate_cap_noted flag used to swallow it as None.
                seq = self._cand_seq
                self._cand_seq += 1
                self.candidates.append({
                    "cid": f"c{seq}", "seq": seq, "kind": kind, "title": title,
                    "artist": artist, "detail": detail, "payload": payload or {},
                    "selected": selected,
                })
                cid = f"c{seq}"
        if capped:  # log once, outside the lock (push_line takes it itself)
            self.push_line(
                f"Reached the {self.CANDIDATE_CAP:,}-result cap — further "
                "finds aren't listed. Narrow the scan (scan by artist) to "
                "see the rest.")
        return cid

    @property
    def candidate_cap_hit(self) -> bool:
        """True once a scan hit CANDIDATE_CAP and stopped listing further finds,
        so summaries can say results were truncated instead of implying the full
        found-count is reviewable."""
        return self._candidate_cap_noted

    def selected_candidates(self) -> list:
        return [c for c in self.candidates if c.get("selected")]

    # ── selection (server-backed; the review UI no longer trusts form ticks) ──
    def set_selected(self, cid: str, on: bool) -> bool:
        """Flip one candidate's selected flag. Returns True if a row changed."""
        with self._lock:
            for c in self.candidates:
                if c.get("cid") == cid:
                    changed = bool(c.get("selected")) != bool(on)
                    c["selected"] = bool(on)
                    return changed
        return False

    def set_all_selected(self, on: bool, cids=None) -> int:
        """Set selected across every candidate, or just those in ``cids`` (a
        single page). Returns how many rows changed."""
        want = set(cids) if cids is not None else None
        n = 0
        with self._lock:
            for c in self.candidates:
                if want is not None and c.get("cid") not in want:
                    continue
                if bool(c.get("selected")) != bool(on):
                    c["selected"] = bool(on)
                    n += 1
        return n

    def selection_counts(self) -> dict:
        """Authoritative review tallies for the UI — computed server-side so a
        paginated page (which holds only some checkboxes) still shows the true
        whole-set numbers. ``reclaimable`` sums est_saving over selected
        candidates for the downsample review's "space reclaimed" figure."""
        with self._lock:
            cands = list(self.candidates)
        selected = [c for c in cands if c.get("selected")]
        artists = {c.get("artist") for c in cands}
        reclaimable = sum(int((c.get("payload") or {}).get("est_saving") or 0)
                          for c in selected)
        return {
            "total": len(cands),
            "artists": len(artists),
            "selected": len(selected),
            "reclaimable": reclaimable,
        }


class JobRegistry:
    """In-memory store for all jobs, bounded to the last N finished jobs."""

    MAX_FINISHED = 50

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def add(self, job: Job):
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()
        job_persistence.persist(job)
        job_persistence.prune_finished(self.PERSIST_KEEP)

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return [self._jobs[jid] for jid in self._order if jid in self._jobs]

    def pending_and_running(self) -> list[Job]:
        return [j for j in self.all() if j.status in ACTIVE]

    def awaiting_review(self) -> list[Job]:
        return [j for j in self.all()
                if j.status == JobStatus.AWAITING_REVIEW]

    def executing(self) -> list[Job]:
        """Jobs actively reading cfg/touching files RIGHT NOW (not merely queued
        or parked). Used to decide whether it's safe to apply a deferred config
        change without mutating an in-flight job underneath the other lane."""
        return [j for j in self.all()
                if j.status in (JobStatus.RUNNING, JobStatus.SCANNING)]

    def finished(self) -> list[Job]:
        return [j for j in self.all() if j.status in TERMINAL]

    PERSIST_KEEP = 1000  # finished rows retained on disk for the archive view

    def _prune_locked(self):
        """Evict the oldest finished jobs past MAX_FINISHED. Caller holds _lock.

        Keeps the most recently finished — a restart rehydrates the on-disk
        archive in creation order, so ordering by finish time (not insertion)
        is what makes "the last N finished" hold there too. The SQLite row
        stays after eviction so /jobs/{id} can still render the archive view;
        only "Clear finished" deletes from disk (see clear_finished)."""
        finished = [self._jobs[jid] for jid in self._order
                    if jid in self._jobs and self._jobs[jid].status in TERMINAL]
        excess = len(finished) - self.MAX_FINISHED
        if excess <= 0:
            return
        finished.sort(key=lambda j: j.finished_at or j.created_at)
        for job in finished:
            if excess <= 0:
                break
            # Don't evict a job a client is still streaming/viewing — that would
            # 404 it out from under them. It's pruned once the stream closes.
            if job._subscribers:
                continue
            self._order.remove(job.id)
            self._jobs.pop(job.id, None)
            excess -= 1

    def clear_finished(self):
        """Drop every job in a terminal state. Active jobs are kept."""
        with self._lock:
            keep = [jid for jid in self._order
                    if self._jobs.get(jid) and self._jobs[jid].status not in TERMINAL]
            dropped = [jid for jid in self._jobs.keys() if jid not in keep]
            for jid in dropped:
                self._jobs.pop(jid, None)
            self._order = keep
        for jid in dropped:
            job_persistence.delete(jid)


# ── Logging capture ───────────────────────────────────────────────────────────

class JobLogHandler(logging.Handler):
    """Routes records from the shared qobuz_librarian logger to a Job."""

    _ANSI = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, job: Job):
        super().__init__()
        self.job = job

    def emit(self, record: logging.LogRecord):
        # Both worker lanes (download + scan) attach a handler to the SAME
        # process-global "qobuz_librarian" logger, and Python dispatches every
        # record to EVERY handler regardless of the emitting thread. Without this
        # filter a download's lines (incl. [ERROR]) bleed into a concurrent
        # scan's log/SSE stream and vice versa. _TLS.current_job identifies the
        # emitting thread's job (set in _run_task, and propagated into pool
        # workers via pool_initializer_kwargs), so only push records that belong
        # to THIS handler's job.
        if getattr(_TLS, "current_job", None) is not self.job:
            return
        try:
            self.job.push_line(self._ANSI.sub("", self.format(record)))
        except Exception:
            pass


# ── Global singletons ─────────────────────────────────────────────────────────

registry = JobRegistry()
# Two worker lanes so a quick single-album download isn't stuck behind a scan
# whose execute phase is downloading 30 albums. Each lane keeps its own work
# queue and worker thread; the shared `staging_lock` below serialises the
# parts that actually touch /staging (rip + beets), so the two lanes safely
# interleave per-album rather than per-job. Scans themselves don't take the
# lock, so a download can run while a scan is mid-walk.
_download_worker_thread: Optional[threading.Thread] = None
_scan_worker_thread: Optional[threading.Thread] = None
_download_queue: "queue.Queue" = queue.Queue()
_scan_queue: "queue.Queue" = queue.Queue()
_stop_event = threading.Event()
# Mutual exclusion around the actual rip+import work. Acquired per album in
# the execute loops (and once around each single-album download), released
# between albums so the other lane gets a turn. Streamrip writes per-album
# subfolders into /staging and beets has a process-wide SQLite lock — running
# both lanes in parallel without this would race on both.
_staging_lock = threading.Lock()


def staging_lock():
    """Return the staging-mutex object so callers can ``with staging_lock():``.

    Held while a single album is being downloaded and imported. Release
    between albums (or between a download and an import phase) lets the
    other worker lane interleave its own album-level work instead of
    waiting for an entire batch.
    """
    return _staging_lock


def _friendly_job_error(exc, fallback: str) -> str:
    """Map common worker failures to a short user-facing summary.

    The raw error text remains in job.log_lines for the expandable log;
    job.error is what the red banner shows."""
    from qobuz_librarian.api.auth import (
        AuthLost,
        NoCredsError,
        QobuzError,
        QobuzUnavailable,
    )
    if isinstance(exc, NoCredsError):
        return "No Qobuz credentials set — visit Settings."
    if isinstance(exc, AuthLost):
        return "Token is expired or invalid — update it in Settings."
    if isinstance(exc, QobuzUnavailable):
        return "Qobuz is temporarily unavailable (network or rate limit) — try again shortly."
    if isinstance(exc, QobuzError):
        return "Couldn't reach the Qobuz API — check the container's network."
    if isinstance(exc, FileNotFoundError):
        # job.error is rendered through Jinja autoescape, so don't escape here
        # too (that double-encodes characters like & in a path).
        fname = str(exc.filename) if exc.filename else "see log"
        return f"Required tool or path missing — see log ({fname})."
    if isinstance(exc, OSError):
        import errno
        if exc.errno == errno.ENOSPC:
            return "Out of disk space — free space and retry."
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return ("Permission denied writing to the staging or music dir — "
                    "check PUID/PGID match the volume owner.")
    return fallback


def _run_task(job: Job, fn):
    """Run one phase of a job with log capture and status bookkeeping."""
    handler = JobLogHandler(job)
    handler.setFormatter(logging.Formatter("%(message)s"))
    app_logger = logging.getLogger("qobuz_librarian")
    app_logger.addHandler(handler)
    _TLS.current_job = job
    try:
        fn(job)
        if (job.cancel_requested and job.status not in TERMINAL
                and job.status != JobStatus.AWAITING_REVIEW):
            # A cooperative fn returned early on the cancel flag. A scan that
            # reached AWAITING_REVIEW finished and parked results — a cancel
            # landing just after that mustn't discard them; cancel_review is
            # the explicit path for dismissing a parked review.
            job.status = JobStatus.CANCELED
        # fn may have parked the job in AWAITING_REVIEW (scan with results);
        # only auto-complete a job that's still RUNNING.
        elif job.status == JobStatus.RUNNING:
            job.status = JobStatus.DONE
    except (Exception, SystemExit) as e:
        # SystemExit too: load_qobuz_token() exits when credentials are
        # missing; in a worker thread that must surface as a failed job,
        # not a silently dead worker.
        # Some exit messages embed ANSI escapes (fmt(C.RED, ...)) which
        # would render as literal `\x1b[91m...` in the web UI — strip
        # them so the error banner is readable.
        raw = str(e) or e.__class__.__name__
        cleaned = JobLogHandler._ANSI.sub("", raw).strip()
        job.status = JobStatus.FAILED
        job.error = _friendly_job_error(e, cleaned)
        job.push_line(f"[ERROR] {cleaned}")
    finally:
        _TLS.current_job = None
        app_logger.removeHandler(handler)
        if job.status in TERMINAL:
            job.finished_at = time.time()
            _fire_post_job_hook(job)
        # Persist outside the post-job hook (and after AWAITING_REVIEW
        # transitions inside scan_fn) so the saved row matches what the user
        # would see on /queue, including candidate lists for review jobs.
        # Skip if the job was evicted from the registry while we ran — a
        # terminal transition above can race a concurrent Clear-History (which
        # deletes the row), and a blind persist would re-insert the just-cleared
        # job onto the History page.
        if registry.get(job.id) is not None:
            job_persistence.persist(job)
        job.end_stream()


def _fire_post_job_hook(job):
    """Run the POST_JOB_HOOK command (if set) with the job's final state on
    stdin as JSON. Errors are logged via vlog and never raised — a broken
    hook can't kill the worker."""
    import json
    import os
    import subprocess

    from qobuz_librarian.ui_cli.logging import vlog
    cmd = os.environ.get("POST_JOB_HOOK", "").strip()
    if not cmd:
        return
    payload = json.dumps({
        "id": job.id,
        "status": job.status.value,
        "title": job.title,
        "artist": job.artist,
        "error": job.error,
        "finished_at": job.finished_at,
    })
    from qobuz_librarian import config as _cfg
    try:
        proc = subprocess.Popen(
            ["sh", "-c", cmd],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        vlog(f"post-job hook failed: {e}")
        return
    try:
        proc.communicate(payload.encode("utf-8"), timeout=_cfg.POST_JOB_HOOK_TIMEOUT)
    except subprocess.TimeoutExpired:
        # communicate() doesn't kill or reap on timeout — without this the shell
        # child and its open stdin pipe linger as a zombie under PID 1.
        proc.kill()
        proc.communicate()
        vlog("post-job hook timed out")


def _worker_loop(work_queue: "queue.Queue"):
    while not _stop_event.is_set():
        try:
            job, fn = work_queue.get(timeout=1)
        except queue.Empty:
            # Idle tick: apply any deferred settings change so `current()`
            # on the Settings page reflects reality even when no new job
            # has been submitted.
            try:
                from qobuz_librarian.web import settings_store
                # Only drain when nothing is active anywhere: the OTHER worker
                # lane may be mid-job, and applying a deferred quality/config
                # change on this idle tick is exactly the mid-job mutation
                # save() deferred to prevent. (The pre-job drain below is the
                # intended apply point once a lane picks up the next job.)
                if not settings_store._any_active_job():
                    settings_store.drain_pending()
            except Exception:
                pass
            continue
        # Settings changes deferred while we were busy are applied BEFORE the
        # next job so "apply now" takes effect for it — but NOT while the OTHER
        # lane is mid-execution. This lane's job is still PENDING here (it goes
        # RUNNING below), so registry.executing() reflects only a concurrently
        # RUNNING/SCANNING job in the other lane, which is reading cfg per-item
        # right now; applying config under it is exactly the mid-job mutation
        # save() deferred. When the other lane is busy, the change stays deferred
        # and the idle tick (or the next pre-job point) applies it once idle.
        try:
            from qobuz_librarian.web import settings_store
            if not registry.executing():
                settings_store.drain_pending()
        except Exception:
            pass
        # Sole worker thread — catching BaseException ensures one
        # crashed job can't take down the whole queue.
        try:
            job.status = JobStatus.RUNNING
            if job.started_at is None:
                job.started_at = time.time()
            job_persistence.persist(job)
            _run_task(job, fn)
        except BaseException as e:  # noqa: BLE001 - must not die
            try:
                if job.status not in TERMINAL:
                    job.status = JobStatus.FAILED
                    import traceback as _tb
                    summary = _tb.format_exception_only(type(e), e)[-1].strip()
                    job.error = f"Worker crash: {summary} — restart the job."
                logging.getLogger("qobuz_librarian").exception(
                    "worker: job %s crashed hard", job.id)
            except Exception:
                pass
        finally:
            try:
                work_queue.task_done()
            except ValueError:
                pass


def start_worker():
    global _download_worker_thread, _scan_worker_thread
    _stop_event.clear()
    if not (_download_worker_thread and _download_worker_thread.is_alive()):
        _download_worker_thread = threading.Thread(
            target=_worker_loop, args=(_download_queue,), daemon=True,
            name="job-worker-download")
        _download_worker_thread.start()
    if not (_scan_worker_thread and _scan_worker_thread.is_alive()):
        _scan_worker_thread = threading.Thread(
            target=_worker_loop, args=(_scan_queue,), daemon=True,
            name="job-worker-scan")
        _scan_worker_thread.start()


def submit(job: Job, fn):
    """Queue a simple job. fn(job) runs to completion on the download worker."""
    registry.add(job)
    _download_queue.put((job, fn))
    return job


def submit_scan(job: Job, scan_fn, execute_fn):
    """Queue a scan/review/execute job.

    scan_fn(job) attaches candidates via job.add_candidate(). If it finds
    any, the job parks in AWAITING_REVIEW for the user to pick from; if it
    finds none, the job completes immediately. execute_fn(job, selected)
    runs later, once approve() is called.
    """
    job.kind = "scan"
    job._execute_fn = execute_fn
    registry.add(job)

    def _scan(j: Job):
        j.status = JobStatus.SCANNING
        if j.started_at is None:
            j.started_at = time.time()
        j.phase = "scan"
        # Record SCANNING on disk so a restart mid-crawl restores through the
        # neutral "interrupted, resumes where it left off" path rather than the
        # harsher running-job "submit again" one (the worker only ever persisted
        # the preceding RUNNING).
        job_persistence.persist(j)
        scan_fn(j)
        if j.status != JobStatus.RUNNING and j.status != JobStatus.SCANNING:
            return  # scan_fn already set a terminal/explicit status
        if j.cancel_requested:
            return  # _run_task will detect the flag and set CANCELED
        if j.selected_candidates() or j.candidates:
            j.status = JobStatus.AWAITING_REVIEW
        else:
            j.push_line("Nothing to do — no candidates found.")
            j.status = JobStatus.DONE

    _scan_queue.put((job, _scan))
    return job


def approve(job: Job, selected_ids=None) -> bool:
    """Resume a reviewed job: run execute_fn over the selected candidates.

    Selection is server-backed — each tick is saved as it happens — so the web
    approve passes selected_ids=None and the already-saved `selected` flags
    drive the run. selected_ids is still honoured when given (the CLI/tests set
    the selection at approve time); None means "use whatever is already saved."
    Returns False if the job isn't awaiting review or has no execute function.
    """
    # Flip the status under the job lock so a second concurrent approve
    # (double-click, two tabs) loses the check and can't enqueue the execute
    # phase a second time — which would re-download and re-import every album.
    # The registry and work queue are in-memory, so a process death here loses
    # the whole job anyway; there's nothing to orphan.
    with job._lock:
        if job.status != JobStatus.AWAITING_REVIEW or job._execute_fn is None:
            return False
        job.status = JobStatus.PENDING
        job.finished_at = None
        if selected_ids is not None:
            keep = set(selected_ids)
            for c in job.candidates:
                c["selected"] = c["cid"] in keep
    job_persistence.persist(job)

    def _execute(j: Job):
        # Cancelled between approve and the worker picking this up: don't start
        # the work. _run_task sees the flag on a still-RUNNING job and marks it
        # CANCELED.
        if j.cancel_requested:
            return
        j.phase = "execute"
        # Read the selection at run time, not at approve time. A hide/dismiss
        # can land in the window between approve flipping the status and the
        # worker starting this (its own status guard and mutation straddle an
        # await), and dismiss drops those candidates from j.candidates under the
        # same lock. Reading here means an album hidden in that window isn't
        # downloaded — a stale approve-time snapshot would queue it anyway.
        with j._lock:
            chosen = j.selected_candidates()
        if not chosen:
            j.push_line("No candidates selected — nothing to do.")
            j.status = JobStatus.DONE
            return
        j._execute_fn(j, chosen)

    _scan_queue.put((job, _execute))
    return True


def cancel_review(job: Job) -> bool:
    """Discard a job that's waiting for review without executing anything."""
    # Flip under the job lock so this can't race approve() (which also flips
    # AWAITING_REVIEW under the lock); otherwise a cancel could land after an
    # approve already queued the execute phase, showing CANCELED while work
    # runs. end_stream() is called outside the lock — it re-acquires the lock
    # to fan out, so calling it inside would deadlock.
    with job._lock:
        if job.status != JobStatus.AWAITING_REVIEW:
            return False
        job.status = JobStatus.CANCELED
        job.finished_at = time.time()
    job_persistence.persist(job)
    job.end_stream()
    return True


def restore_jobs(execute_registry: dict) -> None:
    """Rehydrate the registry from the on-disk job table at app startup.

    ``execute_registry`` maps an ``execute_kind`` string to a factory
    ``factory(job, execute_args)`` that returns the bound execute_fn to use
    when the user later approves a reloaded AWAITING_REVIEW job. The
    factory is invoked lazily, on approve — so it can re-read a fresh
    Qobuz token (the one captured at original-submit time is gone).

    Three behaviours by saved status:
      * DONE / FAILED / CANCELED — loaded as-is so /queue still shows them.
      * AWAITING_REVIEW — loaded with candidates intact, execute_fn rebound
        from ``execute_registry`` if the kind is known. If the kind is
        unknown (registry change between releases), the job is rebadged
        FAILED with a hint to re-run the original scan.
      * PENDING / RUNNING / SCANNING — the closures that drove them are
        gone, so they're rebadged FAILED("interrupted on restart — submit
        again") and saved back. The user sees them rather than them
        silently vanishing.
    """
    job_persistence.init()
    rows = job_persistence.load_all()
    if not rows:
        return
    interrupted = 0
    review = 0
    historical = 0
    restored = []
    for row in rows:
        try:
            status = JobStatus(row["status"])
        except ValueError:
            # Unknown status in the file — drop the row rather than letting
            # one bad entry block startup.
            job_persistence.delete(row["id"])
            continue
        job = Job(
            id=row["id"],
            title=row.get("title") or "",
            artist=row.get("artist") or "",
            album_id=row.get("album_id") or "",
            kind=row.get("kind") or "download",
            status=status,
            phase=row.get("phase") or "",
            candidates=row.get("candidates") or [],
            error=row.get("error"),
            summary=row.get("summary") or "",
            review_verb=row.get("review_verb") or "Download",
            execute_kind=row.get("execute_kind") or "",
            execute_args=row.get("execute_args") or {},
            single=row.get("single") or {},
            created_at=row.get("created_at") or time.time(),
            finished_at=row.get("finished_at"),
        )
        if status in (JobStatus.PENDING, JobStatus.RUNNING):
            job.status = JobStatus.FAILED
            job.error = ("Interrupted by a container restart — submit this "
                         "job again to retry.")
            job.finished_at = time.time()
            interrupted += 1
            job_persistence.persist(job)
        elif status == JobStatus.SCANNING:
            # A scan caught mid-crawl isn't a failure — record it as cancelled
            # (neutral) with a note in the summary, not a red error. A library
            # scan keeps a checkpoint and continues from where it stopped.
            job.status = JobStatus.CANCELED
            # Library scans auto-resume from their checkpoint when the app next
            # opens; the whole-library repair sweep also checkpoints but only
            # picks up when its scan is started again; every other kind restarts.
            if job.execute_kind == "library":
                job.summary = ("Interrupted by a restart — it resumes from where "
                               "it left off the next time you open the app.")
            elif job.execute_kind == "repair":
                job.summary = ("Interrupted by a restart — start the repair scan "
                               "again and it continues from where it left off.")
            else:
                job.summary = "Interrupted by a restart — run the scan again to retry."
            job.finished_at = time.time()
            interrupted += 1
            job_persistence.persist(job)
        elif status == JobStatus.AWAITING_REVIEW:
            factory = execute_registry.get(job.execute_kind)
            if factory is None:
                job.status = JobStatus.FAILED
                job.error = ("Couldn't restore this job's executor across "
                             "the restart — re-run the original scan.")
                job.finished_at = time.time()
                interrupted += 1
                job_persistence.persist(job)
            else:
                job._execute_fn = factory(job, job.execute_args)
                review += 1
        else:
            historical += 1
        restored.append(job)
    # Insert once, then bound the in-memory finished set to MAX_FINISHED just
    # like a live add() would — the archive on disk keeps up to PERSIST_KEEP so
    # an evicted job still renders via /jobs/{id}. Without this a busy prior run
    # would rehydrate hundreds of finished jobs straight onto /queue.
    with registry._lock:
        for job in restored:
            registry._jobs[job.id] = job
            registry._order.append(job.id)
        registry._prune_locked()
    if interrupted or review:
        logging.getLogger("qobuz_librarian").info(
            "Restored %d historical / %d review / %d interrupted job(s) "
            "from the previous run.", historical, review, interrupted)


def load_historical_job(job_id: str) -> Optional[Job]:
    """Rebuild a Job from the on-disk archive without inserting it back into
    the registry. Used by /jobs/{id} when the live registry doesn't know
    about it, so an evicted (or restart-old) terminal job still gets a
    read-only render instead of silently redirecting to /queue."""
    row = job_persistence.load_one(job_id)
    if row is None:
        return None
    try:
        status = JobStatus(row["status"])
    except ValueError:
        return None
    return Job(
        id=row["id"],
        title=row.get("title") or "",
        artist=row.get("artist") or "",
        album_id=row.get("album_id") or "",
        kind=row.get("kind") or "download",
        status=status,
        phase=row.get("phase") or "",
        candidates=row.get("candidates") or [],
        error=row.get("error"),
        summary=row.get("summary") or "",
        review_verb=row.get("review_verb") or "Download",
        execute_kind=row.get("execute_kind") or "",
        execute_args=row.get("execute_args") or {},
        single=row.get("single") or {},
        created_at=row.get("created_at") or time.time(),
        finished_at=row.get("finished_at"),
    )


def request_cancel(job: Job) -> bool:
    """Stop a job from the UI, whatever phase it's in.

    - awaiting review  → discarded immediately
    - scanning/running → cooperative: the flag is set and the scan/execute
      loops bail at their next iteration (so a long library scan can be
      stopped without restarting the container)
    - pending          → flagged; it'll cancel as soon as the worker picks
      it up

    Returns False only if the job is already finished.
    """
    if job.status == JobStatus.AWAITING_REVIEW and cancel_review(job):
        return True
    # Either it wasn't in review, or an approve() flipped it to PENDING between
    # the check and cancel_review's locked re-check. Fall through to the
    # cooperative flag so that race doesn't swallow the user's cancel.
    if job.status in TERMINAL:
        return False
    job.cancel_requested = True
    return True
