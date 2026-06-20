"""Tests for the web UI: background job system (jobs.py) and HTTP routes (app.py)."""
import queue
import time

import pytest

from qobuz_librarian.web import jobs as jm

# ── jobs.py: Job ──────────────────────────────────────────────────────────────



def test_log_lines_capped_with_truncation_marker():
    job = jm.Job()
    total = jm.Job.LOG_CAP + jm.Job._LOG_SLACK + 1
    for i in range(total):
        job.push_line(f"line{i}")
    assert len(job.log_lines) == jm.Job.LOG_CAP
    assert job.log_lines[0] == jm.Job._TRUNCATION_MARKER
    assert job.log_lines[-1] == f"line{total - 1}"


def test_push_line_strips_control_bytes():
    job = jm.Job()
    job.push_line("hello\x00world\x07\x1bend")
    assert job.log_lines == ["helloworldend"]
    # \t and \n are preserved (tabs in subprocess output, newlines re-emitted).
    job.push_line("a\tb\nc")
    assert job.log_lines[-1] == "a\tb\nc"


def test_slow_subscriber_keeps_live_tail_and_close_marker():
    """A consumer that falls behind (its queue fills) must keep getting the
    newest lines and the closing marker rather than being dropped and freezing
    on a stream that never ends."""
    job = jm.Job()
    slow = queue.Queue(maxsize=3)
    job._subscribers.append(slow)
    for i in range(10):
        job.push_line(f"line{i}")
    job.end_stream()

    drained = []
    try:
        while True:
            drained.append(slow.get_nowait())
    except queue.Empty:
        pass
    assert slow in job._subscribers          # not evicted on backpressure
    assert jm.STREAM_END in drained          # close marker still delivered
    assert [x for x in drained if x != jm.STREAM_END][-1] == "line9"


# ── jobs.py: JobLogHandler ────────────────────────────────────────────────────

def test_log_handler_strips_ansi_and_routes_by_thread():
    import logging
    job = jm.Job()
    handler = jm.JobLogHandler(job)
    handler.setFormatter(logging.Formatter("%(message)s"))
    rec = logging.LogRecord("x", logging.INFO, "f", 1,
                            "\x1b[32mcolored\x1b[0m text", None, None)
    # The two worker lanes share the global logger, so the handler only captures
    # records emitted while ITS job owns the thread (keyed on _TLS.current_job,
    # as _run_task sets it). A record emitted while another job owns the thread
    # must NOT bleed into this job's log.
    jm._TLS.current_job = job
    try:
        handler.emit(rec)
        jm._TLS.current_job = jm.Job()      # a different job owns the thread now
        handler.emit(logging.LogRecord("x", logging.INFO, "f", 1,
                                       "foreign line", None, None))
    finally:
        jm._TLS.current_job = None          # don't leak into other tests
    assert job.log_lines == ["colored text"]   # foreign line filtered out


def test_helper_thread_inherits_job_for_log_routing():
    # The rip/beets subprocess output-reader threads log via the shared logger;
    # the thread wrapper must carry the spawning job onto them so their lines
    # pass JobLogHandler's per-thread filter instead of being silently dropped.
    import threading
    job = jm.Job()
    captured = {}

    def target():
        captured["job"] = getattr(jm._TLS, "current_job", None)

    jm._TLS.current_job = job
    try:
        wrapped = jm._propagate_job_to_thread(target)   # captures on this thread
    finally:
        jm._TLS.current_job = None
    t = threading.Thread(target=wrapped)
    t.start()
    t.join()
    assert captured["job"] is job        # reader thread saw the spawning job




# ── jobs.py: worker loop ──────────────────────────────────────────────────────

def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False




def test_download_lane_runs_while_scan_lane_busy():
    """A single-album download must not wait behind a busy scan lane —
    that's the whole point of the split worker lanes."""
    import threading

    jm.start_worker()
    scan_running = threading.Event()
    scan_release = threading.Event()
    download_ran = threading.Event()

    scan_job = jm.Job(title="busy scan")
    scan_job.kind = "scan"

    def _busy_scan(j):
        scan_running.set()
        scan_release.wait(timeout=5)

    # Submit straight onto the scan lane (no candidates → submit_scan would
    # short-circuit to DONE before _busy_scan got to wait).
    jm.registry.add(scan_job)
    jm._scan_queue.put((scan_job, _busy_scan))

    assert scan_running.wait(timeout=5), "scan lane never started its job"

    dl_job = jm.Job(title="single download")
    jm.submit(dl_job, lambda j: download_ran.set())

    assert download_ran.wait(timeout=5), \
        "download lane stalled behind a busy scan lane"
    scan_release.set()
    assert _wait_for(lambda: scan_job.status in jm.TERMINAL)


def test_staging_lock_serialises_lane_album_work():
    """Both lanes interleave at the album level: only one rip+import at a
    time, even with two workers running. Guards against /staging races and
    beets' SQLite write lock."""
    import threading

    jm.start_worker()
    inside = threading.Event()
    release = threading.Event()
    second_inside = threading.Event()

    holder = jm.Job(title="lock holder")
    holder.kind = "scan"

    def _hold(j):
        with jm.staging_lock():
            inside.set()
            release.wait(timeout=5)

    jm.registry.add(holder)
    jm._scan_queue.put((holder, _hold))
    assert inside.wait(timeout=5)

    contender = jm.Job(title="lock contender")
    contender_started = threading.Event()

    def _grab(j):
        contender_started.set()   # worker picked up the job; now it blocks on the lock
        with jm.staging_lock():
            second_inside.set()

    jm.submit(contender, _grab)
    # Wait until the worker has actually entered _grab (it is now blocking on
    # staging_lock, which the holder still owns).  Only then assert it can't
    # proceed — otherwise a slow scheduler means the assertion is vacuous.
    assert contender_started.wait(timeout=5), "download-lane worker never picked up contender"
    assert not second_inside.wait(timeout=0.3)
    release.set()
    assert second_inside.wait(timeout=5)


def test_scan_job_parks_for_review_then_executes():
    jm.start_worker()
    executed = {}

    def scan(j):
        j.add_candidate("album", "Album A", "Artist", payload={"id": 1})
        j.add_candidate("album", "Album B", "Artist", payload={"id": 2})

    def execute(j, chosen):
        executed["ids"] = [c["payload"]["id"] for c in chosen]

    job = jm.Job(title="scan")
    jm.submit_scan(job, scan, execute)
    assert _wait_for(lambda: job.status == jm.JobStatus.AWAITING_REVIEW)
    assert len(job.candidates) == 2
    assert jm.approve(job, ["c1"])
    assert _wait_for(lambda: job.status == jm.JobStatus.DONE)
    assert executed["ids"] == [2]


def test_late_cancel_does_not_discard_a_parked_review():
    # A cancel flag arriving just as a scan parks its results must not flip
    # AWAITING_REVIEW to CANCELED — the found candidates would be lost.
    # cancel_review is the explicit path for dismissing a parked review.
    job = jm.Job(title="scan")
    job.status = jm.JobStatus.RUNNING
    job.cancel_requested = True

    def fn(j):
        j.add_candidate("album", "Album A", "Artist", payload={"id": 1})
        j.status = jm.JobStatus.AWAITING_REVIEW

    jm._run_task(job, fn)
    assert job.status == jm.JobStatus.AWAITING_REVIEW
    assert len(job.candidates) == 1


def test_submit_failing_job_marks_failed():
    jm.start_worker()

    def boom(_j):
        raise RuntimeError("kaboom")

    job = jm.Job(title="bad")
    jm.submit(job, boom)
    assert _wait_for(lambda: job.status == jm.JobStatus.FAILED)
    assert job.error == "kaboom"
    assert any("kaboom" in ln for ln in job.log_lines)


def test_approve_flips_status_then_passes_chosen_to_execute(monkeypatch):
    job = jm.Job(title="scan-approve")
    job.kind = "scan"
    job.status = jm.JobStatus.AWAITING_REVIEW
    got_chosen = []
    job._execute_fn = lambda j, chosen: got_chosen.append(chosen)
    job.add_candidate("album", "A", "Artist", payload={"id": 1})

    status_at_put = []
    enqueued = []

    def _spy_put(item):
        status_at_put.append(item[0].status)
        enqueued.append(item[1])

    monkeypatch.setattr(jm._scan_queue, "put", _spy_put)

    assert jm.approve(job, ["c0"]) is True
    # Status flips to PENDING before the execute step is enqueued, so a second
    # concurrent approve can't double-enqueue the download.
    assert status_at_put == [jm.JobStatus.PENDING]
    # Running the enqueued step hands execute_fn exactly the kept candidate.
    enqueued[0](job)
    assert [c["payload"] for c in got_chosen[0]] == [{"id": 1}]
    # A second approve no longer sees AWAITING_REVIEW, so it's rejected.
    assert jm.approve(job, ["c0"]) is False


def test_base_exception_in_job_does_not_kill_worker():
    jm.start_worker()

    def hard(_j):
        raise KeyboardInterrupt("simulated hard failure inside a job")

    bad = jm.Job(title="hard-fail")
    jm.submit(bad, hard)
    assert _wait_for(lambda: bad.status == jm.JobStatus.FAILED)

    ran = []
    ok = jm.Job(title="after")
    jm.submit(ok, lambda j: ran.append(j.id))
    assert _wait_for(lambda: ok.status == jm.JobStatus.DONE)
    assert ran == [ok.id]


# ── app.py: HTTP routes ───────────────────────────────────────────────────────

@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from qobuz_librarian.web.app import app
    with TestClient(app) as c:
        c.get("/")
        token = c.cookies.get("qf_csrf")
        c.headers.update({"X-CSRF-Token": token})
        yield c




def test_download_error_raw_exception_not_reflected(client, monkeypatch):
    """Unexpected errors must not be reflected verbatim in the response."""
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")

    def boom(_id, _token):
        raise RuntimeError("<script>alert(1)</script>")

    monkeypatch.setattr(search_mod, "get_album", boom)
    r = client.post("/download", data={"album_id": "x"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "alert-error" in r.text


def test_downsample_scan_needs_no_credentials(client, monkeypatch):
    # Downsampling is local — the scan route must not gate on a Qobuz token the
    # way Upgrade/Repair do, and it submits a downsample-kind job.
    import qobuz_librarian.web.app as app_mod

    captured = {}
    monkeypatch.setattr(app_mod.job_mgr, "submit_scan",
                        lambda job, scan_fn, execute_fn: captured.update(job=job))
    # No _get_token patch on purpose: a missing token must not block this route.
    r = client.post("/downsample", follow_redirects=False)
    assert r.status_code == 303
    assert "/jobs/" in r.headers["location"]
    assert captured["job"].execute_kind == "downsample"


def test_second_downsample_scan_folds_onto_the_active_one(client, monkeypatch):
    # A double-submit (double-click, or auto-trigger racing a manual click) must
    # fold onto the in-flight scan, not stack a duplicate. The check + submit run
    # atomically under the lock, so even back-to-back POSTs dedupe.
    import qobuz_librarian.web.app as app_mod

    submitted = []

    def fake_submit(job, scan_fn, execute_fn):
        # Stand in for the worker: register the job as active so the next POST's
        # _active_scan sees it (the real worker flips it to SCANNING).
        job.status = jm.JobStatus.SCANNING
        submitted.append(job)
    monkeypatch.setattr(app_mod.job_mgr, "submit_scan", fake_submit)
    monkeypatch.setattr(app_mod.job_mgr.registry, "pending_and_running",
                        lambda: list(submitted))

    first = client.post("/downsample", follow_redirects=False).headers["location"]
    second = client.post("/downsample", follow_redirects=False).headers["location"]
    assert first == second                # second folded onto the first
    assert len(submitted) == 1            # only one scan ever submitted


def test_lyrics_scan_needs_no_credentials(client, monkeypatch):
    # Lyric fetching is local — the route must not gate on a Qobuz token, and it
    # submits a simple run-to-completion job rather than a scan/review.
    import qobuz_librarian.web.app as app_mod

    captured = {}
    monkeypatch.setattr(app_mod.job_mgr, "submit",
                        lambda job, fn: captured.update(job=job))
    r = client.post("/lyrics", data={"synced_only": "on"}, follow_redirects=False)
    assert r.status_code == 303
    assert "/jobs/" in r.headers["location"]
    assert captured["job"].title == "Lyrics backfill"


# ── Per-artist tool routes (Artist page → scoped scans) ────────────────────

def test_upgrade_scan_artist_redirects_to_settings_without_creds(client, monkeypatch):
    # The per-artist upgrade route gates on Qobuz creds the same way the
    # whole-library one does — a click without creds set must bounce to
    # /setup, not silently submit a job that can't talk to Qobuz.
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds", lambda: {})
    r = client.post("/upgrade/artist", data={"artist": "Stars of the Lid"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/settings")


def test_upgrade_scan_artist_happy_path_submits_a_scoped_job(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(app_mod, "_active_scan", lambda *a, **k: None)
    captured = {}
    monkeypatch.setattr(app_mod.job_mgr, "submit_scan",
                        lambda job, scan_fn, execute_fn: captured.update(job=job))
    r = client.post("/upgrade/artist", data={"artist": "Stars of the Lid"},
                    follow_redirects=False)
    assert r.status_code == 303 and "/jobs/" in r.headers["location"]
    job = captured["job"]
    assert job.execute_kind == "upgrade"
    assert job.review_verb == "Upgrade"
    assert job.title == "Quality upgrade scan"
    assert job.artist == "Stars of the Lid"


def test_downsample_scan_artist_needs_no_credentials(client, monkeypatch):
    # Same local-only contract as the whole-library route — a missing token
    # must not block this either, and the job carries the artist + kind.
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_active_scan", lambda *a, **k: None)
    captured = {}
    monkeypatch.setattr(app_mod.job_mgr, "submit_scan",
                        lambda job, scan_fn, execute_fn: captured.update(job=job))
    r = client.post("/downsample/artist", data={"artist": "Burial"},
                    follow_redirects=False)
    assert r.status_code == 303 and "/jobs/" in r.headers["location"]
    assert captured["job"].execute_kind == "downsample"
    assert captured["job"].title == "Downsample scan"
    assert captured["job"].artist == "Burial"


def test_lyrics_scan_artist_needs_no_credentials(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_active_scan", lambda *a, **k: None)
    captured = {}
    monkeypatch.setattr(app_mod.job_mgr, "submit",
                        lambda job, fn: captured.update(job=job))
    r = client.post("/lyrics/artist", data={"artist": "Bonobo"},
                    follow_redirects=False)
    assert r.status_code == 303 and "/jobs/" in r.headers["location"]
    assert captured["job"].title == "Lyrics backfill"
    assert captured["job"].artist == "Bonobo"


def test_repair_scan_artist_redirects_to_settings_without_creds(client, monkeypatch):
    # Repair needs Qobuz (ISRC lookups), same as the whole-library route.
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds", lambda: {})
    r = client.post("/repair/artist", data={"artist": "Crosby, Stills & Nash"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/settings")


def test_per_artist_routes_reject_empty_and_control_chars_with_one_helper(client, monkeypatch):
    # _clean_artist_name backs all five Artist-page POSTs (the existing
    # /artist scan + the four new tool ones). An empty name or a control char
    # must redirect back to /artist with an error flash, not silently submit.
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")  # not reached
    for path in ("/upgrade/artist", "/lyrics/artist",
                 "/repair/artist", "/downsample/artist"):
        r = client.post(path, data={"artist": "  "}, follow_redirects=False)
        assert r.status_code == 303, f"{path} on empty"
        assert "/artist?error=" in r.headers["location"], f"{path} on empty"
        r = client.post(path, data={"artist": "Bad\x00Name"},
                        follow_redirects=False)
        assert r.status_code == 303, f"{path} on control char"
        assert "/artist?error=" in r.headers["location"], f"{path} on control char"


def test_download_partial_result_marks_done_with_error(client, monkeypatch):
    # A partial download (some tracks failed but the album was imported)
    # keeps the job DONE — the folder is reachable — but must surface the
    # failure count in job.error so the green ✓ isn't lying.
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.modes.process as proc_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "partial1", "title": "Partial Album",
        "artist": {"name": "Test Artist"}, "tracks": {"items": []}})
    monkeypatch.setattr(proc_mod, "process_album", lambda *a, **k: {
        "result": "partial", "n_ok": 5, "n_fail": 2, "imported": True})

    jm.start_worker()
    r = client.post("/download", data={"album_id": "partial1"},
                    follow_redirects=False)
    assert r.status_code in (200, 303)

    new_jobs = [j for j in list(jm.registry._jobs.values())
                if getattr(j, "album_id", None) == "partial1"]
    assert len(new_jobs) == 1
    job = new_jobs[0]
    try:
        assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        assert job.status == jm.JobStatus.DONE
        assert job.error and "2 tracks failed" in job.error
    finally:
        _remove_job(job)


def test_download_authlost_branch_redirects_to_settings_error(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.api.auth import AuthLost
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")

    def authlost(_id, _token):
        raise AuthLost("nope")
    monkeypatch.setattr(search_mod, "get_album", authlost)
    r = client.post("/download", data={"album_id": "x"},
                    follow_redirects=False)
    # Either an htmx-style 200 with auth banner or a redirect.
    assert r.status_code in (200, 303)
    body = r.text + (r.headers.get("location") or "")
    assert "auth" in body.lower() or "settings" in body.lower()


def test_download_qobuzerror_branch_uses_generic_message(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.api.auth import QobuzError
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")

    def qberr(_id, _token):
        raise QobuzError("HTTP 500 from album/get: <html>internal sentinel xyz</html>")
    monkeypatch.setattr(search_mod, "get_album", qberr)
    r = client.post("/download", data={"album_id": "x"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "internal sentinel xyz" not in r.text
    assert "alert-error" in r.text


def test_download_transient_branch_says_try_again(client, monkeypatch):
    # A Qobuz outage while resolving the album must read as "temporarily
    # unavailable, try again" — not the generic "check your token", which would
    # send the user chasing a credential problem that doesn't exist.
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.api.auth import QobuzUnavailable
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")

    def unavailable(_id, _token):
        raise QobuzUnavailable("couldn't reach the Qobuz API")
    monkeypatch.setattr(search_mod, "get_album", unavailable)
    r = client.post("/download", data={"album_id": "x"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "alert-error" in r.text
    assert "temporarily unavailable" in r.text.lower()
    assert "token" not in r.text.lower()


def test_search_transient_branch_says_try_again(client, monkeypatch):
    # A Qobuz outage during a text search must read as "temporarily
    # unavailable", not the generic "Search failed" that hides the cause.
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.api.auth import QobuzUnavailable
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")

    def unavailable(q, t, limit=None):
        raise QobuzUnavailable("couldn't reach the Qobuz API")
    monkeypatch.setattr(search_mod, "search_albums", unavailable)
    r = client.post("/search", data={"q": "radiohead"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "temporarily unavailable" in r.text.lower()
    assert "search failed" not in r.text.lower()


def test_search_result_id_is_html_escaped(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "search_albums",
                         lambda q, t, limit=None: [{
                             "id": 'a"b', "title": "T",
                             "artist": {"name": "A"},
                             "tracks_count": 1, "maximum_bit_depth": 24}])
    monkeypatch.setattr(cat, "album_quality_label", lambda a: "x")
    monkeypatch.setattr(cat, "album_year", lambda a: 2020)
    r = client.post("/search", data={"q": "hello"},
                     headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'value="a&#34;b"' in r.text or 'value="a&quot;b"' in r.text
    assert 'value="a"b"' not in r.text


def test_search_artist_url_shows_helpful_error(client, monkeypatch):
    """Artist/interpreter Qobuz URLs must show a helpful error, not empty results."""
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")

    r = client.post("/search",
                    data={"q": "https://www.qobuz.com/us-en/interpreter/test/123"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Only Qobuz album URLs are supported here" in r.text
    assert "No results" not in r.text


def test_search_treats_lookalike_url_as_text(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "search_albums",
                        lambda q, t, limit=None: [])
    r = client.post("/search",
                    data={"q": "https://evil.example.com/qobuz.com/album/x"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Only Qobuz album URLs are supported here" not in r.text




def test_dashboard_no_creds_shows_setup_cta(client, monkeypatch):
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {})
    r = client.get("/")
    assert r.status_code == 200
    assert "Set up your Qobuz account" in r.text


def test_csrf_oversize_body_rejected_before_parse(client):
    r = client.post(
        "/artist",
        headers={"content-length": "2000000",
                 "content-type": "application/x-www-form-urlencoded"},
        content=b"")
    assert r.status_code == 413


def test_audit_redirects_to_repair(client):
    r = client.get("/audit", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/repair"


def test_service_worker_served_from_root(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers.get("service-worker-allowed") == "/"
    assert r.headers.get("cache-control") == "no-cache"
    assert "install" in r.text


def test_settings_save_defers_apply_when_job_is_active(tmp_path, monkeypatch):
    """An in-flight job must not see cfg.* flip mid-run."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)
    monkeypatch.setattr(ss, "_any_active_job", lambda: True)
    with ss._pending_lock:
        ss._pending_apply = None

    ok, _ = ss.save({"AUTO_UPGRADE_ENABLED": True})
    assert ok is True
    assert (tmp_path / "s.json").exists()
    assert cfg.AUTO_UPGRADE_ENABLED is False  # not yet applied

    ss.drain_pending()
    assert cfg.AUTO_UPGRADE_ENABLED is True
    ss.drain_pending()
    assert cfg.AUTO_UPGRADE_ENABLED is True  # idempotent






def test_execute_upgrades_does_not_flip_global_cfg(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import flows

    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)
    seen_args = []

    def fake_process_album(album, args, **kwargs):
        seen_args.append(getattr(args, "auto_upgrade", None))
        # While the run is in flight, cfg.AUTO_UPGRADE_ENABLED must NOT
        # have been flipped to True.
        assert cfg.AUTO_UPGRADE_ENABLED is False
        return {"imported": False, "result": "user_skipped"}

    import qobuz_librarian.modes.process as proc_mod
    monkeypatch.setattr(proc_mod, "process_album", fake_process_album)
    monkeypatch.setattr(flows, "get_album",
                        lambda aid, tok: {"id": aid, "title": "A"})

    class _FakeJob:
        cancel_requested = False
    chosen = [{
        "artist": "Artist", "title": "A",
        "payload": {"album_id": "1"},
    }]
    monkeypatch.setattr(cfg, "ARTIST_API_DELAY", 0.0)
    flows.execute_upgrades(_FakeJob(), chosen, token="tok")

    assert seen_args == [True]
    assert cfg.AUTO_UPGRADE_ENABLED is False


# ── CSRF middleware ───────────────────────────────────────────────────────────

def test_csrf_post_without_token_is_rejected():
    """One representative POST verifies CSRF-missing → 403."""
    from fastapi.testclient import TestClient

    from qobuz_librarian.web.app import app
    with TestClient(app) as c:
        c.get("/")
        r = c.post("/search", data={"q": "anything"})
        assert r.status_code == 403


def test_csrf_form_field_body_replayed_to_route():
    from fastapi.testclient import TestClient

    from qobuz_librarian.web.app import app
    from qobuz_librarian.web.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD
    with TestClient(app) as c:
        c.get("/")
        token = c.cookies.get(CSRF_COOKIE_NAME)
        # Send CSRF token as form field only (no header) — plain HTML form path
        c.headers.pop("X-CSRF-Token", None)
        r = c.post("/artist",
                   data={CSRF_FORM_FIELD: token, "artist": "test-artist"},
                   headers={"X-CSRF-Token": ""},
                   follow_redirects=False)
        # /artist with a non-empty name redirects to /jobs/<id> (303).
        # Anything else means the body got consumed (422) or CSRF rejected
        # (403) or something else broke.
        assert r.status_code == 303, (
            f"Expected 303 redirect, got {r.status_code} — "
            "form body likely consumed before FastAPI read Form(...) params"
        )


# ── app.py: credential helpers ────────────────────────────────────────────────

def test_write_then_read_creds_roundtrip(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp
    cfg_path = tmp_path / "streamrip" / "config.toml"
    monkeypatch.setattr(cfg, "STREAMRIP_CONFIG", cfg_path)
    monkeypatch.setattr(cfg, "QOBUZ_USER_AUTH_TOKEN", "")
    webapp._write_creds("user42", "secret-token")
    assert cfg_path.exists()
    creds = webapp._read_creds()
    assert creds == {"user_id": "user42", "auth_token": "secret-token"}


# ── run-lock busy → destructive routes 503, read-only stay open ───────

def test_lock_busy_refuses_destructive_routes(monkeypatch):
    from fastapi.testclient import TestClient

    from qobuz_librarian.web import app as webapp
    with TestClient(webapp.app) as c:
        c.get("/")
        token = c.cookies.get("qf_csrf")
        c.headers.update({"X-CSRF-Token": token})
        monkeypatch.setattr(webapp, "_LOCK_BUSY_PID", 4321)

        dash = c.get("/")
        assert dash.status_code == 200
        assert "4321" in dash.text

        for path, data in [
            ("/download", {"album_id": "1"}),
            ("/artist", {"artist": "Radiohead"}),
            ("/library", {}),
            ("/upgrade", {}),
            ("/repair", {}),
            ("/lyric-retry", {}),
            ("/jobs/whatever/approve", {}),
        ]:
            r = c.post(path, data=data, follow_redirects=False)
            assert r.status_code == 503, f"{path} should 503 when lock busy"
            # The full-page response should render the base shell, not a bare
            # <pre>, so a non-htmx caller still has navigation back.
            assert "navbar" in r.text, f"{path} should render base.html shell"


def test_lyric_retry_submits_job_and_redirects(client, monkeypatch):
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import flows
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(flows, "run_lyric_retry", lambda j, t: None)
    r = client.post("/lyric-retry", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/jobs/")


# ── gap-fill candidate detail ─────────

class TestGapCandidateDetail:
    # A partly-downloaded album surfaces as a gap-fill candidate; the engine's
    # split of partial vs fully-missing is covered in test_discovery.

    def test_candidate_detail_marks_gap_fill(self, monkeypatch):
        from qobuz_librarian.web import flows
        monkeypatch.setattr(flows, "album_year", lambda a: 2017)
        monkeypatch.setattr(flows, "album_quality_label", lambda a: "Hi-res")

        class FakeJob:
            def __init__(self):
                self.candidates = []

            def add_candidate(self, **kwargs):
                self.candidates.append(kwargs)

        partial_job = FakeJob()
        flows._add_album_candidate(
            partial_job,
            {"id": 2, "title": "Partial", "tracks_count": 10,
             "_partial_missing_count": 3},
            "Test",
        )
        assert "gap-fill: 3 missing" in partial_job.candidates[0]["detail"]

        full_job = FakeJob()
        flows._add_album_candidate(
            full_job,
            {"id": 1, "title": "Missing", "tracks_count": 10},
            "Test",
        )
        assert "gap-fill" not in full_job.candidates[0]["detail"]
        assert "10 tracks" in full_job.candidates[0]["detail"]


# ── imported must gate success counting ─────────

class TestExecuteSuccessCounting:

    def _candidate(self, album_id="A1", album_dir="/tmp/album"):
        return {
            "payload": {"album_id": album_id, "album_dir": album_dir,
                        "artist_name": "Artist", "verified_truncated": []},
            "title": "Album", "artist": "Artist",
        }

    def test_execute_albums_not_counted_when_import_fails(self,
                                                           monkeypatch, caplog):
        import logging

        from qobuz_librarian.web import flows

        monkeypatch.setattr(flows, "get_album",
                            lambda aid, tok: {"id": aid, "title": "T",
                                              "tracks": {"items": []}})
        monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                            lambda *a, **k: {
                                "result": "downloaded", "n_ok": 5,
                                "n_fail": 0, "n_lossy": 0,
                                "imported": False, "auto_upgrade": False})
        monkeypatch.setattr("qobuz_librarian.config.ARTIST_API_DELAY", 0)

        with caplog.at_level(logging.INFO, logger="qobuz_librarian"):
            flows.execute_albums(jm.Job(title="t"), [self._candidate()], "tok")
        assert any("0/1" in r.message for r in caplog.records)

    def test_execute_upgrades_not_counted_when_import_fails(self,
                                                             monkeypatch, caplog):
        """Beets failure must not count as an upgrade."""
        import logging

        from qobuz_librarian.web import flows

        monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                            lambda *a, **k: {
                                "result": "downloaded", "n_ok": 5,
                                "n_fail": 0, "n_lossy": 0,
                                "imported": False, "auto_upgrade": True})
        monkeypatch.setattr(flows, "get_album",
                            lambda aid, tok: {"id": aid, "title": "T"})
        monkeypatch.setattr("qobuz_librarian.config.ARTIST_API_DELAY", 0)
        monkeypatch.setattr("qobuz_librarian.config.AUTO_UPGRADE_ENABLED", False)

        cand = {
            "payload": {"album_id": "A1"},
            "title": "Album", "artist": "Artist",
        }
        with caplog.at_level(logging.INFO, logger="qobuz_librarian"):
            flows.execute_upgrades(jm.Job(title="t"), [cand], "tok")
        assert any("0/1" in r.message for r in caplog.records)

    def test_execute_propagates_auth_loss_instead_of_swallowing_it(self, monkeypatch):
        # A token that drops mid-batch must abort so the worker shows the
        # "token expired" banner — not be caught as one album's failure while
        # every later album fails the same way and the job still ends "done".
        from qobuz_librarian.api.auth import AuthLost
        from qobuz_librarian.web import flows

        monkeypatch.setattr(flows, "get_album",
                            lambda aid, tok: {"id": aid, "title": "T"})

        def expired(*a, **k):
            raise AuthLost("401")

        monkeypatch.setattr("qobuz_librarian.modes.process.process_album", expired)
        monkeypatch.setattr("qobuz_librarian.config.ARTIST_API_DELAY", 0)

        with pytest.raises(AuthLost):
            flows.execute_albums(jm.Job(title="t"), [self._candidate()], "tok")


# ── Settings diagnostics card ────────────────────────────────────────

def test_diagnostics_reports_paths_and_binaries(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import app as webapp
    music = tmp_path / "music"
    music.mkdir()
    (music / "Some Artist").mkdir()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "not_mounted")
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", tmp_path / "nope" / "library.db")

    by_label = {c["label"]: c for c in webapp._diagnostics()}
    assert by_label["Music library (MUSIC_ROOT)"]["ok"] is True
    assert by_label["Staging (STAGING_DIR)"]["ok"] is False
    # A beets DB whose parent dir is unmounted is flagged with a clear reason.
    beets_row = by_label["beets DB (BEETS_DB_PATH)"]
    assert beets_row["ok"] is False and "does not exist" in beets_row["detail"]
    assert "`rip` binary" in by_label and "`beet` binary" in by_label
    assert "`flac` binary" in by_label and "`ffmpeg` binary" in by_label


def test_settings_empty_save_with_no_existing_creds_returns_error(client, monkeypatch):
    import qobuz_librarian.web.app as _app
    monkeypatch.delenv("QOBUZ_USER_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(_app, "_read_creds", lambda: {})
    r = client.post("/settings", data={"user_id": "", "auth_token": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?error=empty"


def test_streamrip_quality_stays_int_after_settings_save(monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    settings_store._apply({"STREAMRIP_QUALITY": "3"})
    assert cfg.STREAMRIP_QUALITY == 3
    assert isinstance(cfg.STREAMRIP_QUALITY, int)


def test_lyrics_providers_normalized_and_unknowns_dropped(monkeypatch):
    # A typo'd or unknown provider would otherwise reach syncedlyrics and
    # silently fetch nothing; known names are kept (canonical casing) and
    # the rest dropped.
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store
    monkeypatch.setattr(cfg, "LYRICS_PROVIDERS", [], raising=False)
    settings_store._apply({"LYRICS_PROVIDERS": "lrclib, notaprovider, MUSIXMATCH"})
    assert cfg.LYRICS_PROVIDERS == ["Lrclib", "Musixmatch"]


def test_rejected_token_is_not_saved(client, monkeypatch):
    import qobuz_librarian.api.client as client_mod
    import qobuz_librarian.web.app as _app
    from qobuz_librarian.api.auth import AuthLost
    monkeypatch.delenv("QOBUZ_USER_AUTH_TOKEN", raising=False)
    wrote = []
    monkeypatch.setattr(_app, "_write_creds", lambda *a: wrote.append(a) or True)

    def reject(*a, **k):
        raise AuthLost("invalid token")
    monkeypatch.setattr(client_mod, "qobuz_get", reject)
    r = client.post("/settings", data={"user_id": "u", "auth_token": "tok"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "wasn't saved" in r.text
    assert wrote == []


def test_accepted_token_saves_connects_and_clears_stale_banner(client, monkeypatch):
    import qobuz_librarian.api.client as client_mod
    import qobuz_librarian.web.app as _app
    monkeypatch.delenv("QOBUZ_USER_AUTH_TOKEN", raising=False)
    wrote = []
    monkeypatch.setattr(_app, "_write_creds", lambda *a: wrote.append(a) or True)
    monkeypatch.setattr(client_mod, "qobuz_get", lambda *a, **k: {"albums": {}})
    monkeypatch.setattr(_app, "_TOKEN_VALID", False)   # a stale "invalid" banner is up
    r = client.post("/settings", data={"user_id": "u", "auth_token": "tok"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "connected=1" in loc and "unverified" not in loc
    assert wrote == [("u", "tok")]
    assert _app._TOKEN_VALID is True   # a working save clears the stale banner


def test_unreachable_qobuz_saves_token_but_flags_unverified(client, monkeypatch):
    import qobuz_librarian.api.client as client_mod
    import qobuz_librarian.web.app as _app
    from qobuz_librarian.api.auth import QobuzError
    monkeypatch.delenv("QOBUZ_USER_AUTH_TOKEN", raising=False)
    wrote = []
    monkeypatch.setattr(_app, "_write_creds", lambda *a: wrote.append(a) or True)

    def unreachable(*a, **k):
        raise QobuzError("could not reach Qobuz: connection refused")
    monkeypatch.setattr(client_mod, "qobuz_get", unreachable)
    r = client.post("/settings", data={"user_id": "u", "auth_token": "tok"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "connected=1" in loc and "unverified=1" in loc
    assert wrote == [("u", "tok")]


def test_settings_behavior_persist_failure_redirects_with_error(client, monkeypatch):
    from qobuz_librarian.web import settings_store
    monkeypatch.setattr(settings_store, "save", lambda *_: (False, []))
    r = client.post("/settings/behavior", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert "error=persist" in r.headers["location"]




def test_security_response_headers(client):
    r = client.get("/")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "same-origin"
    csp = r.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    # script-src is nonce-based, not 'unsafe-inline' — so a reflected <script>
    # can't run. The per-request nonce in the header must match the one the
    # page stamps on its inline blocks, or the theme/CSRF/SW scripts all break.
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
    assert "'unsafe-inline'" not in script_src
    nonce = script_src.split("'nonce-", 1)[1].split("'", 1)[0]
    assert f'nonce="{nonce}"' in r.text
    # Don't leak the ASGI framework name.
    assert "server" not in {k.lower() for k in r.headers}
    # HSTS only over https (don't pin a plain-http LAN box into https-only).
    https = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=" in https.headers.get("Strict-Transport-Security", "")
    assert "Strict-Transport-Security" not in r.headers


def test_web_fetch_timeout_honors_env_var(monkeypatch):
    import importlib

    import qobuz_librarian.config as cfg_mod
    monkeypatch.setenv("QL_WEB_FETCH_TIMEOUT", "20")
    monkeypatch.setenv("QL_WEB_TEST_AUTH_TIMEOUT", "5")
    reloaded = importlib.reload(cfg_mod)
    try:
        assert reloaded.WEB_FETCH_TIMEOUT == 20.0
        assert reloaded.WEB_TEST_AUTH_TIMEOUT == 5.0
    finally:
        importlib.reload(cfg_mod)


def test_web_fetch_timeout_floored_against_bad_override(monkeypatch):
    # A zero/negative budget would make every web Qobuz call give up before it
    # started (the deadline is already spent) — clamp it instead of bricking.
    import importlib

    import qobuz_librarian.config as cfg_mod
    monkeypatch.setenv("QL_WEB_FETCH_TIMEOUT", "-5")
    monkeypatch.setenv("QL_WEB_TEST_AUTH_TIMEOUT", "0")
    reloaded = importlib.reload(cfg_mod)
    try:
        assert reloaded.WEB_FETCH_TIMEOUT >= 1.0
        assert reloaded.WEB_TEST_AUTH_TIMEOUT >= 1.0
    finally:
        importlib.reload(cfg_mod)


# ── queue_download: job must be FAILED when process_album fails ───────────────

def _download_job(client, monkeypatch, album_id, process_result):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as webapp

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Album",
                                          "artist": {"name": "Artist"}})
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: process_result)

    r = client.post("/download", data={"album_id": album_id},
                    follow_redirects=False)
    assert r.status_code == 303
    job_id = r.headers["location"].rsplit("/", 1)[1]
    return jm.registry.get(job_id)


def test_queue_download_fails_job_when_import_fails(client, monkeypatch):
    job = _download_job(client, monkeypatch, "qdl1", {
        "result": "downloaded", "n_ok": 0, "n_fail": 3,
        "n_lossy": 0, "imported": False, "auto_upgrade": False,
    })
    assert job is not None
    assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
    assert job.status == jm.JobStatus.FAILED
    assert job.error


def test_queue_download_benign_result_is_done(client, monkeypatch):
    """already_complete and similar non-error results must stay DONE."""
    job = _download_job(client, monkeypatch, "qdl3", {
        "result": "already_complete", "n_ok": 0, "n_fail": 0,
        "n_lossy": 0, "imported": False, "auto_upgrade": False,
    })
    assert job is not None
    assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
    assert job.status == jm.JobStatus.DONE


# ── _active_job includes SCANNING status ─────────────────────────────────────


# ── per-job cancel button on queue page ───────────────────────────────

def _inject_job(status, title="Test Job"):
    """Add a job directly to the shared registry and return it.
    Caller must remove the job in a finally block."""
    job = jm.Job(title=title, status=status)
    jm.registry.add(job)
    return job


def _remove_job(job):
    with jm.registry._lock:
        jm.registry._jobs.pop(job.id, None)
        try:
            jm.registry._order.remove(job.id)
        except ValueError:
            pass




def test_queue_awaiting_review_job_shows_review_and_cancel(client):
    """An awaiting-review job must show both the Review link and × cancel."""
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    try:
        r = client.get("/queue")
        assert r.status_code == 200
        assert f"/jobs/{job.id}" in r.text
        assert f"/jobs/{job.id}/cancel" in r.text
    finally:
        _remove_job(job)


def test_job_page_renders_archived_job_from_sqlite_after_eviction(client):
    """Evicting a finished job from the in-memory registry used to make
    /jobs/{id} silently 303 back to /queue, even though the row was still in
    jobs.db. Now the page falls back to the persistence layer and shows the
    "archived" banner instead of vanishing the history."""
    from qobuz_librarian.web import job_persistence

    # The session conftest disables persistence so tests don't share a jobs.db.
    # Re-enable it just for this test so the SQLite-fallback path is exercised.
    job_persistence._reset_for_tests()
    job_persistence.init()
    try:
        job = jm.Job(title="Ancient History", status=jm.JobStatus.DONE,
                     summary="Imported 12 tracks across 2 albums.")
        job_persistence.persist(job)
        # Eviction only removes from the registry — the SQLite row stays so
        # the archive view has something to render.
        _remove_job(job)
        assert jm.registry.get(job.id) is None

        r = client.get(f"/jobs/{job.id}")
        assert r.status_code == 200
        assert "Ancient History" in r.text
        assert "Imported 12 tracks across 2 albums." in r.text
        assert "archived" in r.text  # the historical banner copy
    finally:
        job_persistence._disabled = True
        if job_persistence._conn is not None:
            try:
                job_persistence._conn.close()
            except Exception:
                pass
            job_persistence._conn = None


def test_job_single_undo_info_survives_persistence_roundtrip():
    """Job.single (single-track-grab undo info) must persist so the Undo
    affordance survives a container restart instead of silently vanishing."""
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    job_persistence.init()
    try:
        single = {"album_dir": "/music/Artist/Album", "isrc": "USABC1234567",
                  "track_number": 3, "marked_single": True, "created_dir": False}
        job = jm.Job(title="Grabbed a track", status=jm.JobStatus.DONE,
                     single=single)
        job_persistence.persist(job)
        rows = {r["id"]: r for r in job_persistence.load_all()}
        assert rows[job.id]["single"] == single
        assert job_persistence.load_one(job.id)["single"] == single
    finally:
        job_persistence._disabled = True
        if job_persistence._conn is not None:
            try:
                job_persistence._conn.close()
            except Exception:
                pass
            job_persistence._conn = None


def test_history_lists_finished_jobs_newest_first(client):
    """Finished jobs live in the durable archive, not the capped in-memory set —
    the History view pages them newest-first and offers a retry on a failure."""
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    job_persistence.init()
    try:
        older = jm.Job(title="Older Done", status=jm.JobStatus.DONE)
        older.finished_at = 1000.0
        newer = jm.Job(title="Newer Failed", artist="Boards of Canada",
                       status=jm.JobStatus.FAILED)
        newer.finished_at = 2000.0
        newer.album_id = "777"
        newer.error = "import failed"
        for j in (older, newer):
            job_persistence.persist(j)

        r = client.get("/queue/history")
        assert r.status_code == 200
        t = r.text
        assert "Older Done" in t and "Newer Failed" in t
        assert t.index("Newer Failed") < t.index("Older Done")  # newest first
        assert f"/jobs/{newer.id}/retry" in t                   # retry on failure
    finally:
        job_persistence._disabled = True
        if job_persistence._conn is not None:
            try:
                job_persistence._conn.close()
            except Exception:
                pass
            job_persistence._conn = None


def test_review_list_groups_candidates_by_artist(client):
    """The review list renders one collapsed section per artist with its album
    count, keeps a per-group select-all, and drops no candidate id."""
    import re
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    for art, alb in [("Beatles", "Abbey Road"), ("ABBA", "Arrival"),
                     ("Beatles", "Revolver")]:
        job.add_candidate(kind="album", title=alb, artist=art, detail="2020")
    try:
        r = client.get(f"/jobs/{job.id}")
        assert r.status_code == 200
        t = r.text
        flat = re.sub(r"\s+", " ", t)
        # Grouped one section per artist, none auto-expanded (multi-artist stays
        # collapsed; only a lone artist opens by default).
        assert "Beatles" in t and "ABBA" in t
        assert "<details open" not in t
        assert "3 albums across 2 artists" in flat
        assert "2 albums" in flat          # the Beatles group's count
        # Every candidate is still its own checkbox (server-backed selection).
        for cid in ("c0", "c1", "c2"):
            assert f'value="{cid}"' in t
        # Per-artist select-all scoped to its group (the delegated handler in
        # app.js reads data-group-select to bound the toggle to this details).
        assert 'data-group-select="1"' in t and 'data-group-select="0"' in t
    finally:
        _remove_job(job)


def test_library_hide_then_restore_round_trip(client, monkeypatch, tmp_path):
    """Hiding an artist from a library review writes the durable store and drops
    those candidates; the Hidden view then restores them."""
    from qobuz_librarian.library import hidden
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    c_dummy = job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                                payload={"year": "1994"}, selected=False)
    job.add_candidate(kind="album", title="Third", artist="Portishead",
                      payload={"year": "2008"}, selected=False)
    job.add_candidate(kind="album", title="Untrue", artist="Burial",
                      payload={"year": "2007"}, selected=False)
    try:
        # Selection is server-backed: tick Dummy via the select endpoint, then
        # "hide the rest" drops only Portishead's unselected album (Third),
        # keeps the ticked Dummy, and never touches Burial.
        r = client.post(f"/jobs/{job.id}/select",
                        data={"cid": c_dummy, "checked": "1"})
        assert r.status_code == 200 and r.json()["selected"] == 1
        r = client.post(f"/jobs/{job.id}/hide", data={"artist": "Portishead"})
        assert r.status_code == 200
        survivors = {c["artist"] + "/" + c["title"]: c["selected"]
                     for c in job.candidates}
        assert survivors == {"Portishead/Dummy": True, "Burial/Untrue": False}
        store = hidden.load()
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Third", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Burial", "Untrue", store)

        r = client.get("/library/hidden")
        assert r.status_code == 200
        assert "Portishead" in r.text

        r = client.post("/library/hidden/restore", data={"artist": "Portishead"})
        assert r.status_code == 200  # follows the 303 to the Hidden view
        assert hidden.count(hidden.SCOPE_MISSING) == 0
    finally:
        _remove_job(job)


def test_library_hidden_restore_unhides_a_single_album_by_fingerprint(client, monkeypatch, tmp_path):
    """The per-album Restore button on the Hidden page sends one fingerprint;
    only that row clears, the artist's other hides stay put."""
    from qobuz_librarian.library import hidden
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")

    hidden.hide(hidden.SCOPE_MISSING,
                [("Portishead", "Dummy", "1994"),
                 ("Portishead", "Third", "2008")])
    fp_dummy = hidden.album_fingerprint("Portishead", "Dummy")
    fp_third = hidden.album_fingerprint("Portishead", "Third")
    # The page exposes the fingerprint so the form has it to send back.
    r = client.get("/library/hidden")
    assert r.status_code == 200
    assert fp_dummy in r.text

    r = client.post("/library/hidden/restore", data={"fingerprint": fp_dummy})
    assert r.status_code == 200  # follows the 303 to the Hidden view
    store = hidden.load()
    assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
    assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Third", store)
    # And the artist-level Restore-all still clears the rest.
    r = client.post("/library/hidden/restore", data={"artist": "Portishead"})
    assert r.status_code == 200
    assert hidden.count(hidden.SCOPE_MISSING) == 0
    _ = fp_third  # used as a domain-readable handle above


def test_new_release_baseline_survives_a_check_run(tmp_path, monkeypatch):
    # A completed library scan seeds the baseline; a later check (mark_run) must
    # keep baseline_complete set — otherwise the auto-check would gate itself off
    # again right after its first run.
    import qobuz_librarian.config as cfg
    from qobuz_librarian.library import new_releases
    monkeypatch.setattr(cfg, "NEW_RELEASE_STATE_FILE", tmp_path / "nr.json")
    assert not new_releases.is_baseline_complete()
    new_releases.seed_baseline({"art1": ["a", "b"]})
    assert new_releases.is_baseline_complete()
    new_releases.mark_run({"art1": ["a", "b", "c"]})
    assert new_releases.is_baseline_complete()
    assert new_releases.load()["seen"]["art1"] == ["a", "b", "c"]


def test_auto_first_scan_starts_once_then_only_resumes(monkeypatch):
    import qobuz_librarian.web.app as webapp
    from qobuz_librarian.library import new_releases, scan_checkpoint
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    monkeypatch.setattr(webapp, "_CLI_MODE", False)
    monkeypatch.setattr(webapp, "_LOCK_BUSY_PID", None)
    monkeypatch.setattr(webapp, "_TOKEN_VALID", None)
    monkeypatch.setattr(webapp.cfg, "AUTO_LIBRARY_SCAN", True)
    monkeypatch.setattr(webapp.job_mgr, "registry", jm.JobRegistry())
    monkeypatch.setattr(new_releases, "note_auto_scan_attempted", lambda: None)
    started = []
    monkeypatch.setattr(webapp, "_start_library_scan",
                        lambda partial_only=False: started.append(partial_only))

    monkeypatch.setattr(new_releases, "is_baseline_complete", lambda: False)
    monkeypatch.setattr(new_releases, "auto_scan_attempted", lambda: False)
    monkeypatch.setattr(scan_checkpoint, "pending", lambda: None)
    # No baseline, no checkpoint, not yet attempted → start a fresh scan once.
    webapp._maybe_auto_first_scan()
    assert started == [False]

    # Already attempted, still no checkpoint → don't relaunch (no nagging).
    monkeypatch.setattr(new_releases, "auto_scan_attempted", lambda: True)
    webapp._maybe_auto_first_scan()
    assert started == [False]

    # An interrupted scan left a checkpoint → resume it, even though attempted.
    monkeypatch.setattr(scan_checkpoint, "pending",
                        lambda: {"kind": "missing", "done": 4})
    webapp._maybe_auto_first_scan()
    assert started == [False, False]

    # Baseline already complete, or the feature is off → never starts.
    monkeypatch.setattr(new_releases, "is_baseline_complete", lambda: True)
    webapp._maybe_auto_first_scan()
    monkeypatch.setattr(new_releases, "is_baseline_complete", lambda: False)
    monkeypatch.setattr(webapp.cfg, "AUTO_LIBRARY_SCAN", False)
    webapp._maybe_auto_first_scan()
    assert started == [False, False]


def test_start_scan_helpers_dedupe_against_an_active_one(monkeypatch):
    # The manual POST and the dashboard auto-trigger both land in these helpers;
    # neither may stack a second scan on one already queued/running.
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp.job_mgr, "registry", jm.JobRegistry())

    lib = jm.Job(title="lib")
    lib.execute_kind = "library"
    lib.status = jm.JobStatus.SCANNING
    jm.registry.add(lib)
    assert webapp._start_library_scan() is lib
    assert len(jm.registry.all()) == 1

    nr = jm.Job(title="nr")
    nr.execute_kind = "new_releases"
    nr.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(nr)
    assert webapp._start_new_release_check() is nr
    assert len(jm.registry.all()) == 2


def test_rescan_folds_during_scan_but_queues_during_download(client, monkeypatch):
    # Double-submitting a slow whole-library scan while it's still crawling must
    # reuse that job, not stack a second hours-long pass. But once the reviewed
    # batch is downloading, a deliberate new scan must queue — the executing job
    # keeps the same execute_kind, so it must not swallow the re-scan.
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp.job_mgr, "registry", jm.JobRegistry())
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    started = []
    monkeypatch.setattr(webapp.job_mgr, "submit_scan",
                        lambda *a, **k: started.append(1))

    active = jm.Job(title="Quality upgrade scan")
    active.execute_kind = "upgrade"
    active.status = jm.JobStatus.SCANNING
    jm.registry.add(active)

    r = client.post("/upgrade", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/jobs/{active.id}"
    assert started == []

    active.status = jm.JobStatus.RUNNING
    r = client.post("/upgrade", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] != f"/jobs/{active.id}"
    assert started == [1]


def test_navbar_surfaces_a_rejected_token_on_every_page(client, monkeypatch):
    # A token that 401s mid-session must be visible from any page, not only the
    # dashboard — the navbar shows a Settings-linked pill.
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_LOCK_BUSY_PID", None)
    monkeypatch.setattr(webapp, "_TOKEN_VALID", None)
    assert "Reconnect Qobuz" not in client.get("/queue").text
    monkeypatch.setattr(webapp, "_TOKEN_VALID", False)
    body = client.get("/queue").text
    assert "Reconnect Qobuz" in body
    assert 'href="/settings"' in body


def test_scan_checkpoint_round_trip_and_kinds_coexist(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.library import scan_checkpoint
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE", tmp_path / "cp.json")
    assert scan_checkpoint.load("missing") is None and scan_checkpoint.pending() is None
    scan_checkpoint.save("missing", {"Beta", "Alpha"},
                         [{"kind": "album", "title": "X", "artist": "Alpha"}],
                         {"a1": ["id1"]})
    cp = scan_checkpoint.load("missing")
    assert cp["scanned"] == ["Alpha", "Beta"] and cp["seen"] == {"a1": ["id1"]}
    assert len(cp["candidates"]) == 1
    assert scan_checkpoint.pending() == {"kind": "missing", "done": 2}
    # A partial checkpoint coexists; clearing one kind leaves the other intact —
    # a completed missing scan must not wipe an interrupted partial one.
    scan_checkpoint.save("partial", {"Gamma"}, [], {})
    scan_checkpoint.clear("missing")
    assert scan_checkpoint.load("missing") is None
    assert scan_checkpoint.load("partial") is not None
    assert scan_checkpoint.pending() == {"kind": "partial", "done": 1}
    scan_checkpoint.clear("partial")
    assert scan_checkpoint.pending() is None
    # The repair sweep shares this store but resumes on a manual re-run, so it's
    # loadable yet deliberately absent from the dashboard's pending() prompt.
    scan_checkpoint.save("repair", {"Delta"}, [], {})
    assert scan_checkpoint.load("repair")["scanned"] == ["Delta"]
    assert scan_checkpoint.pending() is None
    scan_checkpoint.clear("repair")
    assert scan_checkpoint.load("repair") is None


def test_new_release_check_establishes_baseline(tmp_path, monkeypatch):
    # A full new-release check (mark_run complete=True) establishes the baseline,
    # so a manual check before any library scan unlocks the daily auto-check.
    import qobuz_librarian.config as cfg
    from qobuz_librarian.library import new_releases
    monkeypatch.setattr(cfg, "NEW_RELEASE_STATE_FILE", tmp_path / "nr.json")
    assert not new_releases.is_baseline_complete()
    new_releases.mark_run({"art1": ["a"]}, complete=True)
    assert new_releases.is_baseline_complete()


def test_new_release_hide_writes_missing_scope(client, monkeypatch, tmp_path):
    # A "new releases" review offers the per-artist Hide action; it must actually
    # dismiss into the missing scope — it was a silent no-op when new_releases
    # wasn't in _TRIAGE_KINDS — so a release you reject stops resurfacing.
    from qobuz_librarian.library import hidden
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")

    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "new_releases"
    cid = job.add_candidate(kind="album", title="Hit Me Hard And Soft",
                            artist="Billie Eilish", payload={"year": "2024"},
                            selected=True)
    try:
        # New releases arrive pre-ticked; you untick before hiding (the Hide
        # button only offers to drop what you're not taking).
        client.post(f"/jobs/{job.id}/select", data={"cid": cid, "checked": "0"})
        r = client.post(f"/jobs/{job.id}/hide", data={"artist": "Billie Eilish"})
        assert r.status_code == 200
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Billie Eilish",
                                "Hit Me Hard And Soft", hidden.load())
        assert not job.candidates
    finally:
        _remove_job(job)


def test_candidate_ids_stay_unique_after_a_drop():
    """A live scan appends while a hide drops candidates; ids must never be
    reused, or the dropped album's cid would later point at a different one."""
    job = jm.Job(execute_kind="library")
    a = job.add_candidate("album", "A", "Artist", payload={})
    b = job.add_candidate("album", "B", "Artist", payload={})
    job.candidates = [c for c in job.candidates if c["cid"] != a]  # hide A
    c = job.add_candidate("album", "C", "Artist", payload={})
    assert [a, b, c] == ["c0", "c1", "c2"]
    assert len({x["cid"] for x in job.candidates}) == len(job.candidates)


def test_review_page_paginates_by_artist_and_filters_whole_set(client, monkeypatch):
    """The review page is server-paginated by artist; the filter spans the whole
    set, not just the page on screen."""
    monkeypatch.setattr("qobuz_librarian.web.app.REVIEW_PAGE_ARTISTS", 2)
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    for name in ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]:
        job.add_candidate("album", name + " LP", name, payload={}, selected=False)
    try:
        # Alphabetical: page 1 = Alpha,Beta; page 2 = Delta,Epsilon; page 3 = Gamma.
        r = client.get(f"/jobs/{job.id}/review?page=1")
        assert r.status_code == 200
        assert "Alpha" in r.text and "Beta" in r.text and "Gamma" not in r.text
        assert "Page 1 of 3" in r.text
        r = client.get(f"/jobs/{job.id}/review?page=2")
        assert "Delta" in r.text and "Epsilon" in r.text and "Alpha" not in r.text
        # An out-of-range page clamps into range rather than 500ing.
        r = client.get(f"/jobs/{job.id}/review?page=99")
        assert r.status_code == 200 and "Page 3 of 3" in r.text and "Gamma" in r.text
        # Filter spans the whole set (Epsilon would be on page 2 unfiltered).
        r = client.get(f"/jobs/{job.id}/review?q=epsilon")
        assert "Epsilon" in r.text and "Alpha" not in r.text
    finally:
        _remove_job(job)


def test_select_persists_and_counts_are_authoritative(client):
    """Ticking saves server-side and the response carries whole-set counts."""
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    c1 = job.add_candidate("album", "A", "Alpha", payload={}, selected=False)
    job.add_candidate("album", "B", "Beta", payload={}, selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/select", data={"cid": c1, "checked": "1"})
        assert r.status_code == 200
        body = r.json()
        assert body["selected"] == 1 and body["total"] == 2 and body["artists"] == 2
        # The flag actually persisted on the candidate.
        assert next(c for c in job.candidates if c["cid"] == c1)["selected"] is True
        # Select-all flips everything; deselect-all clears it.
        assert client.post(f"/jobs/{job.id}/select-all",
                           data={"on": "1", "scope": "all"}).json()["selected"] == 2
        assert client.post(f"/jobs/{job.id}/select-all",
                           data={"on": "0", "scope": "all"}).json()["selected"] == 0
    finally:
        _remove_job(job)


def test_hide_works_while_still_scanning(client, monkeypatch, tmp_path):
    """Dismissing an artist must be allowed mid-scan, not only once the walk
    has finished."""
    from qobuz_librarian.library import hidden
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")
    job = _inject_job(jm.JobStatus.SCANNING)
    job.execute_kind = "library"
    # Triage scans add candidates unticked; "Hide all" drops the unselected.
    job.add_candidate("album", "Dummy", "Portishead", payload={"year": "1994"},
                      selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/hide", data={"artist": "Portishead"})
        assert r.status_code == 200
        assert job.candidates == []
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy",
                                hidden.load())
    finally:
        _remove_job(job)


def test_upgrade_hide_writes_upgrade_scope_only(client, monkeypatch, tmp_path):
    """An upgrade dismiss records in the 'upgrade' scope and leaves the
    missing-album scope alone; the upgrade Hidden view restores it."""
    from qobuz_librarian.library import hidden
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "upgrade"
    job.add_candidate("upgrade", "Dummy", "Portishead",
                      payload={"year": "1994", "candidate": {}}, selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/hide", data={"artist": "Portishead"})
        assert r.status_code == 200
        assert job.candidates == []
        store = hidden.load()
        assert hidden.is_hidden(hidden.SCOPE_UPGRADE, "Portishead", "Dummy", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)

        r = client.get("/upgrade/hidden")
        assert r.status_code == 200 and "Portishead" in r.text

        r = client.post("/upgrade/hidden/restore", data={"artist": "Portishead"})
        assert r.status_code == 200
        assert hidden.count(hidden.SCOPE_UPGRADE) == 0
    finally:
        _remove_job(job)


def test_new_since_last_scan_badges_only_additions(monkeypatch, tmp_path):
    monkeypatch.setattr("qobuz_librarian.config.SCAN_SEEN_FILE", tmp_path / "seen.json")
    from qobuz_librarian.web import flows

    first = jm.Job(title="scan")
    first.add_candidate("album", "Dummy", "Portishead", payload={})
    flows._flag_new_since_last_scan(first, "missing")
    # First-ever run is the baseline — nothing is "new".
    assert all(not c["payload"].get("is_new") for c in first.candidates)

    second = jm.Job(title="scan")
    second.add_candidate("album", "Dummy", "Portishead", payload={})   # seen before
    second.add_candidate("album", "Third", "Portishead", payload={})   # appeared since
    flows._flag_new_since_last_scan(second, "missing")
    flags = {c["title"]: c["payload"].get("is_new", False) for c in second.candidates}
    assert flags == {"Dummy": False, "Third": True}


def test_push_progress_streams_found_and_hit_but_not_in_replay():
    import json as _json

    job = jm.Job(title="scan")
    q = job.subscribe()
    job.push_progress("Scanning", 5, 10, "Artist", found=3,
                      hit={"artist": "Artist", "albums": 2})
    line = q.get_nowait()
    payload = _json.loads(line[len(jm.PROGRESS_PREFIX):])
    assert payload["found"] == 3
    assert payload["hit"] == {"artist": "Artist", "albums": 2}
    # The snapshot replayed to a reconnecting client must not carry the one-off
    # hit, or the preview row would be appended twice.
    snap = _json.loads(job._progress_snapshot()[len(jm.PROGRESS_PREFIX):])
    assert snap["found"] == 3 and "hit" not in snap


def test_cancel_check_predicate_reads_current_job_flag():
    # The installed hook returns True only when the worker thread's
    # current job has cancel_requested set.
    assert jm._current_job_cancel_requested() is False

    fake = jm.Job(title="x")
    fake.cancel_requested = True
    jm._TLS.current_job = fake
    try:
        assert jm._current_job_cancel_requested() is True
    finally:
        jm._TLS.current_job = None
    assert jm._current_job_cancel_requested() is False


def test_rip_url_returns_canceled_when_check_fires(monkeypatch):
    # rip_url's polling loop must consult _CANCEL_CHECK between proc.wait
    # iterations and kill+return when the check fires, otherwise a clicked
    # Cancel leaves rip running to completion and the album imports anyway.
    import subprocess

    from qobuz_librarian.integrations import rip

    class FakeProc:
        pid = 99999
        returncode = None
        stdout = iter(())

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("rip", timeout or 1)

        def kill(self):
            FakeProc.killed = True

    FakeProc.killed = False
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(rip, "_kill_process_group",
                        lambda proc: setattr(FakeProc, "killed", True))

    poll_count = [0]

    def cancel_after_two_polls():
        poll_count[0] += 1
        return poll_count[0] >= 2

    monkeypatch.setattr(rip, "_CANCEL_CHECK", cancel_after_two_polls)
    rc, out = rip.rip_url("https://play.qobuz.com/track/x", timeout=10)
    assert rc == 130
    assert "canceled by user" in out
    assert FakeProc.killed is True


def test_job_status_api_returns_json_for_known_and_unknown_job(client):
    job = _inject_job(jm.JobStatus.DONE, title="Test")
    try:
        r = client.get(f"/api/jobs/{job.id}/status")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == job.id
        assert data["status"] == "done"
        assert data["title"] == "Test"
        assert isinstance(data["log_lines"], list)
    finally:
        _remove_job(job)

    r2 = client.get("/api/jobs/nonexistent/status")
    assert r2.status_code == 404
    assert r2.json() == {"detail": "Job not found"}


def test_post_job_hook_receives_terminal_state_as_json(tmp_path, monkeypatch):
    import json

    from qobuz_librarian.web import jobs as _jobs
    out = tmp_path / "hook.log"
    monkeypatch.setenv("POST_JOB_HOOK", f"cat > {out}")

    job = jm.Job(title="HookJob", artist="A")
    job.id = "hook-test-id"
    job.status = jm.JobStatus.DONE
    job.finished_at = 1234.0
    _jobs._fire_post_job_hook(job)
    # _fire_post_job_hook communicate()s the hook, so the file is written by the
    # time it returns — wait for it rather than parse a possibly-empty file.
    assert _wait_for(lambda: out.exists() and out.read_text())
    payload = json.loads(out.read_text())
    assert payload["id"] == "hook-test-id"
    assert payload["status"] == "done"
    assert payload["title"] == "HookJob"


def test_jobs_list_returns_array_and_respects_filter(client):
    pending = _inject_job(jm.JobStatus.PENDING, title="P")
    done = _inject_job(jm.JobStatus.DONE, title="D")
    try:
        r = client.get("/api/jobs")
        assert r.status_code == 200
        data = r.json()
        ids = {j["id"] for j in data["jobs"]}
        assert pending.id in ids and done.id in ids
        # Each entry has no log_lines (list mode) but does carry id/status.
        first = data["jobs"][0]
        assert "id" in first and "status" in first
        assert "log_lines" not in first
        # status filter narrows the result
        r2 = client.get("/api/jobs?status=pending")
        ids2 = {j["id"] for j in r2.json()["jobs"]}
        assert pending.id in ids2 and done.id not in ids2
        # limit caps the response
        r3 = client.get("/api/jobs?limit=1")
        assert len(r3.json()["jobs"]) == 1
        # an unrecognised status filter is a 400, not a silent empty list
        r4 = client.get("/api/jobs?status=garbage")
        assert r4.status_code == 400 and r4.json() == {"detail": "Unknown status filter"}
    finally:
        _remove_job(pending)
        _remove_job(done)


def test_dashboard_null_fetch_log_fields_render_safe(client, monkeypatch):
    import qobuz_librarian.ui_cli.prompts as prompts_mod
    monkeypatch.setattr(
        prompts_mod,
        "_read_fetch_log",
        lambda **kw: [{"ts": "2025-01-01T00:00:00", "artist": None, "title": None,
                       "result": "already_complete", "tracks_downloaded": 0}],
    )
    r = client.get("/")
    assert r.status_code == 200
    assert ">None<" not in r.text
    assert "None —" not in r.text




def test_library_scan_cancel_during_walk_ends_canceled():
    """A scan_fn that sets cancel_requested and returns early must end CANCELED."""
    captured = []

    original_put = jm._scan_queue.put
    jm._scan_queue.put = captured.append
    try:
        job = jm.Job(title="Library gap scan")

        def cancel_mid_walk(j):
            j.cancel_requested = True

        jm.submit_scan(job, cancel_mid_walk, lambda j, chosen: None)
    finally:
        jm._scan_queue.put = original_put

    assert len(captured) == 1
    _, fn = captured[0]
    jm._run_task(job, fn)
    assert job.status == jm.JobStatus.CANCELED


# ── /download duplicate-job rejection ───────────────────────────────

def _inject_album_job(album_id, status=jm.JobStatus.PENDING):
    job = jm.Job(title="T", album_id=album_id, status=status)
    jm.registry.add(job)
    return job


def test_download_dedups_against_queued_jobs_and_scan_candidates(client, monkeypatch):
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    # An album already queued → htmx "Already queued", linking the existing job.
    existing = _inject_album_job("dup-album")
    try:
        r = client.post("/download", data={"album_id": "dup-album"},
                        headers={"HX-Request": "true"})
        assert r.status_code == 200 and "Already queued" in r.text and existing.id in r.text
    finally:
        _remove_job(existing)
    # An album that matches an awaiting-review scan candidate → go to that scan,
    # don't queue a second copy.
    scan = jm.Job(title="Library scan", status=jm.JobStatus.AWAITING_REVIEW)
    scan.candidates = [{"cid": "c1", "payload": {"album_id": "scan-X"}}]
    jm.registry.add(scan)
    try:
        r = client.post("/download", data={"album_id": "scan-X"}, follow_redirects=False)
        assert r.status_code == 303 and scan.id in r.headers["location"]
    finally:
        _remove_job(scan)


# ── SSE stream event delivery ───────────────────────────────────────

def test_sse_stream_404_returns_json(client):
    r = client.get("/api/jobs/no-such-job/stream")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"error": "not found"}


def test_sse_done_event_carries_final_status(client):
    """The done event reports the job's real terminal status so the page can
    flip the badge to failed/canceled instead of assuming success."""
    job = jm.Job(title="failed-job")
    job.status = jm.JobStatus.FAILED
    jm.registry.add(job)
    try:
        with client.stream("GET", f"/api/jobs/{job.id}/stream") as r:
            assert r.status_code == 200
            seen = ""
            for chunk in r.iter_text():
                seen += chunk
                if "event: done" in seen:
                    break
            else:
                pytest.fail("SSE stream never sent 'event: done'")
        assert "data: failed" in seen
    finally:
        _remove_job(job)


def test_sse_terminal_job_sends_done_without_blocking():
    import time as _time

    from qobuz_librarian.web.app import job_stream
    job = jm.Job(title="finished-fast")
    job.status = jm.JobStatus.DONE
    job.push_line("preface")
    jm.registry.add(job)
    try:
        import asyncio

        async def collect():
            resp = await job_stream(job.id)
            chunks = []
            async for c in resp.body_iterator:
                chunks.append(c)
                if "event: done" in c:
                    break
            return chunks

        start = _time.monotonic()
        chunks = asyncio.run(collect())
        elapsed = _time.monotonic() - start
        body = "".join(chunks)
        assert "event: done" in body
        assert "preface" in body
        # Pre-fix this took 500ms+ on the empty-queue timeout; 1.5s gives
        # ample CI headroom while still catching an accidental revert.
        assert elapsed < 1.5, f"terminal-job SSE blocked {elapsed:.2f}s"
    finally:
        _remove_job(job)


def test_sse_stream_done_on_stream_end_sentinel(client):
    """A live STREAM_END line pushed after the stream starts must yield event: done."""
    import threading

    job = jm.Job(title="running-then-end")
    job.status = jm.JobStatus.RUNNING
    jm.registry.add(job)

    def _push_end():
        # Poll until the SSE handler has registered its subscriber — avoids a
        # wall-clock race where end_stream() fires before subscribe() is called
        # on a slow CI box.
        deadline = time.time() + 5.0
        while not job._subscribers and time.time() < deadline:
            time.sleep(0.01)
        job.push_line("midstream")
        job.end_stream()

    t = threading.Thread(target=_push_end)
    t.start()
    try:
        with client.stream("GET", f"/api/jobs/{job.id}/stream") as r:
            assert r.status_code == 200
            seen = ""
            for chunk in r.iter_text():
                seen += chunk
                if "event: done" in seen:
                    break
            else:
                pytest.fail("SSE stream never sent 'event: done' after STREAM_END")
            assert "midstream" in seen
    finally:
        t.join()


def test_sse_stream_closes_cleanly_when_subscriber_raises(client, monkeypatch):
    """sub.get() raising must close the stream, not 500."""
    job = jm.Job(title="broken-subscriber")
    job.status = jm.JobStatus.RUNNING
    jm.registry.add(job)

    class _BadQ:
        def get(self, timeout=None):
            raise RuntimeError("subscriber blew up")

    monkeypatch.setattr(job, "subscribe", lambda: _BadQ())

    with client.stream("GET", f"/api/jobs/{job.id}/stream") as r:
        assert r.status_code == 200
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
    assert b"Internal Server Error" not in body


def test_scan_routes_redirect_to_settings_when_no_creds(client, monkeypatch):
    """POSTing to any scan route without creds redirects to /settings."""
    import qobuz_librarian.web.app as webapp
    def _no_creds():
        raise SystemExit(1)
    monkeypatch.setattr(webapp, "_get_token", _no_creds)
    r = client.post("/library", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?error=creds"


def test_scan_library_propagates_authlost_so_job_fails(monkeypatch, tmp_path):
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])

    def _authlost(*a, **k):
        raise AuthLost("token expired")
    monkeypatch.setattr(flows, "find_missing_for_artist", _authlost)

    job = jm.Job(title="lib scan")
    with pytest.raises(AuthLost):
        flows.scan_library(job, "tok")


def test_cancelled_library_scan_does_not_stamp_last_scan(monkeypatch, tmp_path):
    # A cancelled scan must NOT record "last scanned" — otherwise the dashboard
    # reads as freshly scanned and the automatic new-release check is suppressed.
    from qobuz_librarian.library.discovery import DiscoveryResult
    from qobuz_librarian.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows, "find_missing_for_artist",
                        lambda *a, **k: DiscoveryResult(None, None))
    stamped = []
    monkeypatch.setattr(flows, "_record_last_scan", lambda: stamped.append(1))

    cancelled = jm.Job(title="cancelled scan")
    cancelled.cancel_requested = True
    flows.scan_library(cancelled, "tok")
    assert stamped == []                 # cancelled → never stamped

    flows.scan_library(jm.Job(title="clean scan"), "tok")
    assert stamped == [1]                # a clean finish stamps exactly once


def test_scan_upgrades_collects_every_artist(monkeypatch, tmp_path):
    """The upgrade scan fans artists out across worker threads; every artist's
    candidate must still land on the single-writer candidate list."""
    from qobuz_librarian.web import flows

    artists = []
    for i in range(12):
        d = tmp_path / f"Artist {i}"
        d.mkdir()
        artists.append(d)

    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "list_library_artists", lambda: artists)
    monkeypatch.setattr("qobuz_librarian.config.ARTIST_SCAN_WORKERS", 8)
    monkeypatch.setattr("qobuz_librarian.quality.decision.load_capped",
                        lambda: set())

    def _one_upgrade(name, artist_dir, token, args, capped=None):
        return [{"qobuz_album": {"title": f"{name} Album"},
                 "n_present": 0, "n_total": 0,
                 "existing_quality_label": "CD",
                 "target_quality_label": "Hi-Res"}]
    monkeypatch.setattr("qobuz_librarian.quality.decision.scan_artist_for_upgrades",
                        _one_upgrade)

    job = jm.Job(title="upgrade scan")
    flows.scan_upgrades(job, "tok")

    titles = sorted(c["title"] for c in job.candidates)
    assert titles == sorted(f"Artist {i} Album" for i in range(12))


def test_dashboard_does_not_double_surface_awaiting_review(client, monkeypatch):
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp.job_mgr, "registry", jm.JobRegistry())
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})

    review_job = jm.Job(title="Scan A", status=jm.JobStatus.AWAITING_REVIEW)
    jm.registry.add(review_job)
    try:
        r = client.get("/")
        assert r.status_code == 200
        assert "jobs queued" not in r.text
        assert "job queued" not in r.text
        # The review card still appears.
        assert "Waiting for your review" in r.text
    finally:
        _remove_job(review_job)


def test_auto_new_release_check_fires_only_when_due_and_idle(monkeypatch):
    import qobuz_librarian.web.app as webapp
    from qobuz_librarian.library import new_releases
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    monkeypatch.setattr(webapp, "_CLI_MODE", False)
    monkeypatch.setattr(webapp, "_LOCK_BUSY_PID", None)
    monkeypatch.setattr(webapp, "_TOKEN_VALID", None)
    monkeypatch.setattr(webapp.cfg, "NEW_RELEASE_CHECK_INTERVAL", 3600)
    monkeypatch.setattr(webapp.job_mgr, "registry", jm.JobRegistry())
    monkeypatch.setattr(new_releases, "touch_run", lambda: None)
    monkeypatch.setattr(new_releases, "is_baseline_complete", lambda: True)
    fired = []
    monkeypatch.setattr(webapp, "_start_new_release_check", lambda: fired.append(1))

    # Never checked and nothing running → fires.
    monkeypatch.setattr(new_releases, "last_run", lambda: None)
    webapp._maybe_auto_check_new_releases()
    assert len(fired) == 1

    # Checked moments ago → throttled.
    monkeypatch.setattr(new_releases, "last_run", lambda: time.time())
    webapp._maybe_auto_check_new_releases()
    assert len(fired) == 1

    # Due again, but a scan is actively running → skipped.
    monkeypatch.setattr(new_releases, "last_run", lambda: 0)
    busy = jm.Job(title="busy", status=jm.JobStatus.SCANNING)
    jm.registry.add(busy)
    webapp._maybe_auto_check_new_releases()
    assert len(fired) == 1
    busy.status = jm.JobStatus.DONE

    # An earlier new-release list still awaiting review → don't stack another.
    pending = jm.Job(title="New-release check", status=jm.JobStatus.AWAITING_REVIEW)
    pending.execute_kind = "new_releases"
    jm.registry.add(pending)
    webapp._maybe_auto_check_new_releases()
    assert len(fired) == 1
    pending.status = jm.JobStatus.DONE

    # A token Qobuz is already rejecting → don't fire (it would fail every load).
    monkeypatch.setattr(webapp, "_TOKEN_VALID", False)
    webapp._maybe_auto_check_new_releases()
    assert len(fired) == 1
    monkeypatch.setattr(webapp, "_TOKEN_VALID", None)

    # No baseline yet (no full library scan) → dormant, even when due.
    monkeypatch.setattr(new_releases, "is_baseline_complete", lambda: False)
    webapp._maybe_auto_check_new_releases()
    assert len(fired) == 1
    monkeypatch.setattr(new_releases, "is_baseline_complete", lambda: True)

    # Turned off entirely → never fires, even when due.
    monkeypatch.setattr(webapp.cfg, "NEW_RELEASE_CHECK_INTERVAL", 0)
    webapp._maybe_auto_check_new_releases()
    assert len(fired) == 1


def test_empty_search_renders_instructional_hint(client, monkeypatch):
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    r = client.post("/search", data={"q": "   "})
    assert r.status_code == 200
    assert "Type an artist, album, or Qobuz URL" in r.text




def test_sse_stream_emits_heartbeat_when_idle(client, monkeypatch):
    import threading

    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_SSE_HEARTBEAT_TICKS", 1)

    job = jm.Job(title="idle")
    job.status = jm.JobStatus.RUNNING
    jm.registry.add(job)

    def _end_later():
        time.sleep(2.0)  # Give the generator enough empty ticks (each ~0.5s) for a heartbeat.
        job.end_stream()

    t = threading.Thread(target=_end_later)
    t.start()
    try:
        with client.stream("GET", f"/api/jobs/{job.id}/stream") as r:
            assert r.status_code == 200
            seen = ""
            for chunk in r.iter_text():
                seen += chunk
                if "event: done" in seen:
                    break
        assert ": ping" in seen
    finally:
        t.join()


def test_settings_page_does_not_leak_auth_token(client, monkeypatch):
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"user_id": "42", "auth_token": "SECRET-TOKEN-XYZ"})
    r = client.get("/settings")
    assert r.status_code == 200
    assert "SECRET-TOKEN-XYZ" not in r.text
    assert "Token is set" in r.text


def test_run_task_maps_exceptions_to_friendly_errors():
    import errno

    from qobuz_librarian.api.auth import AuthLost
    # AuthLost → a "token expired" message, with the raw detail kept in the log.
    job = jm.Job(title="x")
    jm._run_task(job, lambda j: (_ for _ in ()).throw(AuthLost("401")))
    assert job.status == jm.JobStatus.FAILED
    assert "Token is expired or invalid" in job.error and "401" in job.log_lines[-1]
    # ENOSPC → "out of disk space" instead of a raw OSError string.
    job2 = jm.Job(title="x")
    jm._run_task(job2, lambda j: (_ for _ in ()).throw(
        OSError(errno.ENOSPC, "No space left on device")))
    assert job2.status == jm.JobStatus.FAILED and "Out of disk space" in job2.error


def test_queue_cancel_pending_cancels_all_pending(client):
    p1 = jm.Job(title="A", status=jm.JobStatus.PENDING)
    p2 = jm.Job(title="B", status=jm.JobStatus.PENDING)
    jm.registry.add(p1)
    jm.registry.add(p2)
    try:
        r = client.post("/queue/cancel-pending", follow_redirects=False)
        assert r.status_code == 303
        assert p1.cancel_requested is True
        assert p2.cancel_requested is True
    finally:
        _remove_job(p1)
        _remove_job(p2)


def test_job_retry_resubmits_only_a_failed_job(client, monkeypatch):
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(
        "qobuz_librarian.api.search.get_album",
        lambda aid, tok: {"title": "Album", "artist": {"name": "Artist"}, "id": aid})
    captured = {}
    original_submit = jm.submit
    monkeypatch.setattr(jm, "submit", lambda job, fn: captured.__setitem__("job", job))

    failed = jm.Job(title="Album", album_id="abc", status=jm.JobStatus.FAILED)
    failed.error = "old failure"
    jm.registry.add(failed)
    # Distinct album id so it doesn't dedup-collide with the failed job's retry.
    pending = jm.Job(title="X", album_id="xyz", status=jm.JobStatus.PENDING)
    jm.registry.add(pending)
    try:
        # A failed job resubmits as a fresh job pointing at the same album.
        r = client.post(f"/jobs/{failed.id}/retry", follow_redirects=False)
        assert r.status_code == 303
        new_job = captured.get("job")
        assert new_job is not None and new_job.album_id == "abc" and new_job.id != failed.id
        assert new_job.id in r.headers["location"]
        # A non-failed job is bounced back to the queue, not resubmitted.
        r2 = client.post(f"/jobs/{pending.id}/retry", follow_redirects=False)
        assert r2.status_code == 303 and r2.headers["location"] == "/queue"
    finally:
        _remove_job(failed)
        _remove_job(pending)
        if captured.get("job"):
            _remove_job(captured["job"])
        jm.submit = original_submit






def test_search_query_too_long_is_rejected_or_truncated(client):
    """A megabyte search string must not reach the Qobuz API verbatim."""
    r = client.post("/search", data={"q": "a" * 10_000},
                    headers={"HX-Request": "true"})
    assert r.status_code in (200, 422)




def test_persistence_restores_awaiting_review_with_candidates(monkeypatch):
    """The headline reliability win: a completed scan's candidates survive a
    container restart — the user can still approve them instead of re-scanning
    from artist 1."""
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    # Simulate a scan that parked AWAITING_REVIEW before the container died.
    saved = jm.Job(title="Artist scan", artist="Foo")
    saved.kind = "scan"
    saved.execute_kind = "album"
    saved.status = jm.JobStatus.AWAITING_REVIEW
    saved.add_candidate("album", "Bar", "Foo", payload={"album_id": "abc"})
    job_persistence.persist(saved)

    # Drop the in-memory state to mimic the new process.
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())

    executed = {}

    def _factory(job, _args):
        return lambda j, chosen: executed.setdefault("ids", [
            c["payload"]["album_id"] for c in chosen])

    jm.restore_jobs({"album": _factory})

    restored = jm.registry.get(saved.id)
    assert restored is not None
    assert restored.status == jm.JobStatus.AWAITING_REVIEW
    assert len(restored.candidates) == 1
    assert restored.candidates[0]["payload"] == {"album_id": "abc"}

    # And the user can still approve — the execute_fn was rebound from the
    # kind registry rather than vanishing with the dead closure.
    jm.start_worker()
    assert jm.approve(restored, ["c0"]) is True
    assert _wait_for(lambda: restored.status == jm.JobStatus.DONE)
    assert executed.get("ids") == ["abc"]


def test_persist_survives_non_json_candidate_payload(monkeypatch):
    """A stray non-JSON value in a candidate payload (a Path, say) must coerce
    to text at the write boundary, not raise TypeError — that escaped the
    sqlite guard, killed the worker, and lost the whole parked review."""
    from pathlib import Path

    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    job = jm.Job(title="Review")
    job.kind = "scan"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job.add_candidate("album", "Album", "Artist",
                      payload={"album_id": "A1", "dir": Path("/music/A/B")})
    job_persistence.persist(job)

    row = job_persistence.load_one(job.id)
    assert row is not None
    assert row["candidates"][0]["payload"]["album_id"] == "A1"


def test_persistence_rebadges_inflight_jobs_on_restore(monkeypatch):
    """In-flight jobs from before a restart come back sensibly: a running/pending
    job as FAILED with a retry hint; a scan caught mid-crawl as the neutral
    CANCELED with an 'interrupted' note (not an alarming red failure) — and a
    library scan's note says it resumes."""
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    for status in (jm.JobStatus.RUNNING, jm.JobStatus.PENDING):
        j = jm.Job(title=f"in-flight {status.value}")
        j.status = status
        job_persistence.persist(j)
    generic = jm.Job(title="generic scan")
    generic.status = jm.JobStatus.SCANNING
    job_persistence.persist(generic)
    libscan = jm.Job(title="library scan")
    libscan.status = jm.JobStatus.SCANNING
    libscan.execute_kind = "library"
    job_persistence.persist(libscan)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({})

    by_title = {j.title: j for j in jm.registry.all()}
    for status in ("running", "pending"):
        j = by_title[f"in-flight {status}"]
        assert j.status == jm.JobStatus.FAILED and "Interrupted" in (j.error or "")
    g = by_title["generic scan"]
    assert g.status == jm.JobStatus.CANCELED and not g.error
    assert "Interrupted" in (g.summary or "")
    lib = by_title["library scan"]
    assert lib.status == jm.JobStatus.CANCELED and "resumes" in (lib.summary or "")


def test_persistence_unknown_execute_kind_fails_clean(monkeypatch):
    """A registry mismatch across releases (kind that no longer exists)
    rebadges the job as FAILED rather than silently leaving it un-resumable."""
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    j = jm.Job(title="weird scan")
    j.kind = "scan"
    j.execute_kind = "made-up-kind"
    j.status = jm.JobStatus.AWAITING_REVIEW
    j.add_candidate("album", "x", "Artist", payload={"album_id": "1"})
    job_persistence.persist(j)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({"album": lambda *a: (lambda j, c: None)})

    restored = jm.registry.get(j.id)
    assert restored.status == jm.JobStatus.FAILED
    assert "re-run" in (restored.error or "").lower()


def test_scan_phase_is_persisted_for_restart_resume(monkeypatch):
    """The worker persists RUNNING before a job's fn runs; a scan then flips
    itself to SCANNING. That transition has to reach disk too, or a restart
    mid-crawl restores the scan as a hard 'submit again' failure instead of the
    neutral resumable state the SCANNING restore branch is written for."""
    import threading

    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    jm.start_worker()
    scanning = threading.Event()
    release = threading.Event()

    def scan(j):
        scanning.set()
        release.wait(5)
        j.add_candidate("album", "x")

    job = jm.Job(title="mid-crawl", execute_kind="library")
    jm.submit_scan(job, scan, lambda j, c: None)
    try:
        assert scanning.wait(5)
        row = job_persistence.load_one(job.id)
        assert row and row["status"] == jm.JobStatus.SCANNING.value
    finally:
        release.set()
    assert _wait_for(lambda: job.status == jm.JobStatus.AWAITING_REVIEW)


def test_restore_bounds_in_memory_finished_to_max(monkeypatch):
    """A busy prior run can leave hundreds of finished rows on disk. Restore
    keeps only the most-recently-finished MAX_FINISHED in memory; the rest stay
    on disk for /jobs/{id} rather than all landing on /queue at once."""
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    reg = jm.JobRegistry()
    monkeypatch.setattr(reg, "MAX_FINISHED", 5)
    monkeypatch.setattr(jm, "registry", reg)

    ids = []
    for i in range(12):
        j = jm.Job(title=f"old{i}", status=jm.JobStatus.DONE)
        j.finished_at = 1000.0 + i           # old0 oldest … old11 newest
        ids.append(j.id)
        job_persistence.persist(j)

    jm.restore_jobs({})
    assert {j.title for j in reg.finished()} == {f"old{i}" for i in range(7, 12)}
    assert reg.get(ids[0]) is None                      # evicted from memory
    assert job_persistence.load_one(ids[0]) is not None  # still in the archive


def test_dashboard_token_invalid_shows_banner(client, monkeypatch):
    """The stale-token banner fires only when _TOKEN_VALID is explicitly False."""
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", False)
    r = client.get("/")
    assert r.status_code == 200
    assert "saved token isn't authenticating" in r.text


def test_api_401_flips_dashboard_token_state(monkeypatch):
    """A mid-session 401 from the Qobuz API must flip _TOKEN_VALID via the
    registered listener, so the dashboard doesn't keep showing 'connected'
    after the saved token has stopped working."""
    import qobuz_librarian.api.auth as auth_mod
    import qobuz_librarian.api.client as client_mod
    import qobuz_librarian.web.app as webapp

    class _FakeResp:
        status_code = 401
        text = ""
        headers: dict = {}

    class _FakeSession:
        def get(self, *_a, **_kw):
            return _FakeResp()

    monkeypatch.setattr(client_mod, "_get_session", lambda: _FakeSession())
    monkeypatch.setattr(webapp, "_TOKEN_VALID", True)
    # Register only for the duration of the test so the global hook list
    # doesn't grow across the suite.
    monkeypatch.setattr(auth_mod, "_auth_state_listeners",
                        [webapp._on_auth_state])

    try:
        client_mod.qobuz_get("album/get", {"album_id": "x"}, "tok")
    except auth_mod.AuthLost:
        pass
    else:
        raise AssertionError("expected AuthLost from 401")
    assert webapp._TOKEN_VALID is False


# ── empty form values reach friendly branches, never 422 JSON ──────


def test_download_bad_album_id_renders_friendly_error(client, monkeypatch):
    # Empty id → inline "Missing album id" error.
    r = client.post("/download", data={"album_id": ""}, headers={"HX-Request": "true"})
    assert r.status_code in (200, 400)
    assert r.headers["content-type"].startswith("text/html")
    assert "alert-error" in r.text and "Missing album id" in r.text
    # A 404 from Qobuz → "no album with that id", not a scary network message.
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as webapp
    from qobuz_librarian.api.auth import QobuzError
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album",
                        lambda _id, _tok: (_ for _ in ()).throw(QobuzError(
                            "HTTP 404 from album/get: {'status':'error','code':404}")))
    r2 = client.post("/download", data={"album_id": "ghost-album"},
                     headers={"HX-Request": "true"})
    assert r2.status_code == 200
    assert "No album with that id" in r2.text and "container's network" not in r2.text


def test_artist_empty_form_redirects_with_friendly_error(client):
    r = client.post("/artist", data={"artist": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/artist?error=")
    assert "required" in loc


# ── HEAD requests on /healthz / /queue / /settings ─────────────────


# ── dedupe vs already-in-library ────────────────────────────


def test_download_force_param_overrides_library_check(client, monkeypatch):
    from pathlib import Path

    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat
    import qobuz_librarian.modes.process as proc_mod
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Forced",
                                          "artist": {"name": "Artist"}})
    monkeypatch.setattr(cat, "find_album_dir_filesystem",
                        lambda album: Path("/tmp/album-dir"))
    monkeypatch.setattr(cat, "find_existing_tracks",
                        lambda album, **_kw: ([{"isrc": "X1"}], Path("/tmp/album-dir")))
    monkeypatch.setattr(proc_mod, "process_album",
                        lambda *a, **k: {"result": "downloaded", "n_ok": 1,
                                         "n_fail": 0, "n_lossy": 0,
                                         "imported": True, "auto_upgrade": False})
    r = client.post("/download",
                    data={"album_id": "force-test", "force": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/jobs/")
    new_job_id = loc.rsplit("/", 1)[1]
    job = jm.registry.get(new_job_id)
    try:
        assert job is not None
        assert job.album_id == "force-test"
    finally:
        if job is not None:
            _remove_job(job)


# ── library mode=partial_fill dispatches an album-fill job ──




# ── behavior POST without form_complete preserves untouched keys


def test_settings_behavior_partial_post_preserves_other_booleans(
        tmp_path, monkeypatch, client):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "COMPRESS_ENABLED", True)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    with ss._pending_lock:
        ss._pending_apply = None
    try:
        r = client.post("/settings/behavior",
                        data={"AUTO_UPGRADE_ENABLED": "on"},
                        follow_redirects=False)
        assert r.status_code == 303
        # Partial POST: COMPRESS_ENABLED must stay True.
        assert cfg.COMPRESS_ENABLED is True
        assert cfg.AUTO_UPGRADE_ENABLED is True
    finally:
        with ss._pending_lock:
            ss._pending_apply = None


# ── invalid status filter on /api/jobs returns 400 ──────────


# ── XSS-like artist names rejected before reaching Qobuz ────


def test_artist_with_angle_brackets_rejected(client):
    before = len(jm.registry.all())
    r = client.post("/artist",
                    data={"artist": "<script>alert(1)</script>"},
                    follow_redirects=False)
    # 303 so the browser actually follows the Location to the error banner.
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert before == len(jm.registry.all())


# ── web gap-fill discovery + completeness guard ────────────────────────────────


def test_download_partial_album_proceeds_to_gap_fill(client, monkeypatch):
    from pathlib import Path

    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.modes.process as proc_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    album = {"id": "gap1", "title": "Gappy", "artist": {"name": "A"},
             "tracks": {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}}
    monkeypatch.setattr(search_mod, "get_album", lambda _i, _t: album)
    monkeypatch.setattr(cat_mod, "find_album_dir_filesystem",
                        lambda _a: Path("/music/A/Gappy"))
    monkeypatch.setattr(cat_mod, "find_existing_tracks",
                        lambda _a, **_kw: ([{"id": 1}], None))
    monkeypatch.setattr(cat_mod, "compute_missing",
                        lambda q, e: ([{"id": 2}, {"id": 3}], [{"id": 1}]))
    monkeypatch.setattr(proc_mod, "process_album",
                        lambda *a, **k: {"result": "downloaded",
                                         "imported": True, "n_fail": 0})

    jm.start_worker()
    r = client.post("/download", data={"album_id": "gap1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "already complete" not in r.text.lower()
    new_jobs = [j for j in list(jm.registry._jobs.values())
                if getattr(j, "album_id", None) == "gap1"]
    assert len(new_jobs) == 1
    job = new_jobs[0]
    try:
        _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
    finally:
        _remove_job(job)


def test_download_complete_album_is_blocked(client, monkeypatch):
    """A genuinely complete album is still short-circuited with a friendly message — no job queued."""
    from pathlib import Path

    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    album = {"id": "done1", "title": "Whole", "artist": {"name": "A"},
             "tracks": {"items": [{"id": 1}, {"id": 2}]}}
    monkeypatch.setattr(search_mod, "get_album", lambda _i, _t: album)
    monkeypatch.setattr(cat_mod, "find_album_dir_filesystem",
                        lambda _a: Path("/music/A/Whole"))
    monkeypatch.setattr(cat_mod, "find_existing_tracks",
                        lambda _a, **_kw: ([{"id": 1}, {"id": 2}], None))
    monkeypatch.setattr(cat_mod, "compute_missing",
                        lambda q, e: ([], [{"id": 1}, {"id": 2}]))

    r = client.post("/download", data={"album_id": "done1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "already own" in r.text.lower()
    assert not [j for j in list(jm.registry._jobs.values())
                if getattr(j, "album_id", None) == "done1"]


def test_settings_save_rejects_out_of_enum_quality(tmp_path, monkeypatch):
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)

    assert ss.save({"STREAMRIP_QUALITY": "99"})[0] is True
    on_disk = json.loads((tmp_path / "s.json").read_text())
    assert on_disk.get("STREAMRIP_QUALITY") != "99"
    # A valid value still persists.
    assert ss.save({"STREAMRIP_QUALITY": "2"})[0] is True
    assert json.loads((tmp_path / "s.json").read_text())["STREAMRIP_QUALITY"] == "2"


def test_settings_drops_uninstalled_beets_plugins_and_reports(tmp_path, monkeypatch):
    # A plugin name beets can't load would break every import; it's dropped at
    # save time and called out, instead of silently persisting and poisoning
    # imports library-wide.
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
    # Pin the installed set so the test doesn't ride on what beets ships.
    monkeypatch.setattr(ss, "_available_beets_plugins",
                        lambda: {"fetchart", "lastgenre", "scrub"})

    ok, warnings = ss.save({"BEETS_PLUGINS": "fetchart, nope, LastGenre"})
    assert ok is True
    # Known names survive (LastGenre canonicalised); the bogus one is gone.
    assert cfg.BEETS_PLUGINS == ["fetchart", "lastgenre"]
    assert json.loads((tmp_path / "s.json").read_text())["BEETS_PLUGINS"] == "fetchart,lastgenre"
    assert warnings and "nope" in warnings[0]


# ── search results render cover art ────────────────────────────────────────────


def test_search_rejects_non_qobuz_cover_url(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "search_albums",
                        lambda q, t, limit=None: [{
                            "id": "alb1", "title": "T", "artist": {"name": "A"},
                            "tracks_count": 10, "maximum_bit_depth": 24,
                            "image": {"small": "https://evil.example/x.jpg"}}])
    r = client.post("/search", data={"q": "hello"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "evil.example" not in r.text


# ── CLI/web mode hand-off ───────────────────────────────────────────────────────


def test_mode_handoff_to_cli_pauses_web_downloads(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod
    # No active job (the registry is a shared singleton across tests).
    monkeypatch.setattr(app_mod.job_mgr.registry, "pending_and_running",
                        lambda: [])
    r = client.post("/settings/mode", data={"target": "cli"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?mode=cli"
    assert app_mod._CLI_MODE is True
    # The banner shows everywhere, and download/scan endpoints are paused.
    assert "Terminal (CLI) mode" in client.get("/").text
    blocked = client.post("/download", data={"album_id": "123"},
                          follow_redirects=False)
    assert blocked.status_code == 503 and "Terminal (CLI) mode" in blocked.text
    # Resume restores web mode.
    back = client.post("/settings/mode", data={"target": "web"},
                       follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/settings?mode=web"
    assert app_mod._CLI_MODE is False


def test_mode_handoff_refused_while_a_job_is_active(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod
    monkeypatch.setattr(app_mod.job_mgr.registry, "pending_and_running",
                        lambda: [object()])
    r = client.post("/settings/mode", data={"target": "cli"},
                    follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert app_mod._CLI_MODE is False


def test_qf_cli_only_env_starts_in_cli_mode(monkeypatch):
    monkeypatch.setenv("QL_CLI_ONLY", "1")
    from fastapi.testclient import TestClient

    import qobuz_librarian.web.app as app_mod
    with TestClient(app_mod.app) as c:
        assert app_mod._CLI_MODE is True
        c.get("/")
        tok = c.cookies.get("qf_csrf")
        r = c.post("/download", data={"album_id": "x", "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 503 and "Terminal (CLI) mode" in r.text


def test_settings_save_requires_user_id_with_token(client, monkeypatch):
    """A token with no user id passes the API probe and the Test button, but
    streamrip's login() and load_qobuz_token() both require the user id — so
    the save must refuse instead of writing a config that tests green yet
    fails with 'no credentials' on the first search. The refusal re-renders
    the form so the long token the user pasted survives."""
    import qobuz_librarian.web.app as app_mod

    monkeypatch.delenv("QOBUZ_USER_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(app_mod, "_read_creds", lambda: {})
    wrote = []
    monkeypatch.setattr(app_mod, "_write_creds",
                        lambda uid, tok: wrote.append((uid, tok)) or True)

    r = client.post("/settings",
                    data={"user_id": "", "auth_token": "a-real-looking-token"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "Add your User ID" in r.text
    assert "a-real-looking-token" in r.text
    assert wrote == []


# ── web/auth.py: optional login ────────────────────────────────────────────────


def _enable_auth(monkeypatch, tmp_path, *, configure=True):
    """Turn auth on for one test against an isolated credential file. Returns
    a TestClient bound to the app. The session-wide conftest default of
    WEB_AUTH=none is restored on teardown by monkeypatch."""
    from fastapi.testclient import TestClient

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import auth as web_auth
    from qobuz_librarian.web.app import app

    monkeypatch.setenv("WEB_AUTH", "")
    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "web_auth.json")
    if configure:
        assert web_auth.set_credentials("admin", "hunter2hunter")
    return TestClient(app)


def test_verify_login_matches_only_the_right_pair(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "web_auth.json")
    assert web_auth.set_credentials("admin", "hunter2hunter")
    assert web_auth.verify_login("admin", "hunter2hunter")
    assert not web_auth.verify_login("admin", "wrong")
    assert not web_auth.verify_login("someoneelse", "hunter2hunter")
    # The stored hash is a salted PBKDF2 digest, never the plaintext.
    assert "hunter2hunter" not in (tmp_path / "web_auth.json").read_text()


def test_credential_compare_tolerates_non_ascii(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "web_auth.json")
    assert web_auth.set_credentials("admin", "hunter2hunter")
    # A unicode username or a junk session cookie must compare to a clean
    # False — not raise the TypeError compare_digest throws on non-ASCII str.
    assert web_auth.verify_login("café", "hunter2hunter") is False
    assert web_auth.verify_session("\x80not-the-secret") is False
    assert web_auth.verify_login("admin", "hunter2hunter") is True


def test_logged_out_request_redirects_to_login(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


def test_login_rejects_wrong_password(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("qf_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "nope",
                         "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 401
        assert "qf_session" not in r.cookies
        # Still locked out afterwards.
        assert c.get("/", follow_redirects=False).status_code == 303


def test_login_accepts_correct_password(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        c.get("/login")
        tok = c.cookies.get("qf_csrf")
        r = c.post("/login",
                   data={"username": "admin", "password": "hunter2hunter",
                         "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        # The session cookie now opens a protected route.
        assert c.get("/", follow_redirects=False).status_code == 200


def test_env_credentials_seed_and_reset_the_login(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import auth as web_auth

    monkeypatch.setattr(cfg, "WEB_AUTH_FILE", tmp_path / "web_auth.json")
    monkeypatch.setenv("WEB_AUTH", "")  # auth on
    monkeypatch.setenv("WEB_AUTH_USER", "admin")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "hunter2hunter")
    # Nothing configured yet — the environment seeds the login.
    assert web_auth.apply_env_credentials() == "applied"
    assert web_auth.verify_login("admin", "hunter2hunter")
    # A restart with the same values keeps the session secret untouched.
    secret = web_auth.session_value()
    assert web_auth.apply_env_credentials() == "unchanged"
    assert web_auth.session_value() == secret
    # Changing the password re-seeds and rotates the secret (logs browsers out).
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "a-different-password")
    assert web_auth.apply_env_credentials() == "applied"
    assert web_auth.verify_login("admin", "a-different-password")
    assert web_auth.session_value() != secret
    # Only half the pair is a misconfiguration, not a silent half-login.
    monkeypatch.delenv("WEB_AUTH_PASSWORD")
    assert web_auth.apply_env_credentials() == "partial"


def test_repeated_failed_logins_are_throttled(monkeypatch, tmp_path):
    from qobuz_librarian.web import auth as web_auth
    web_auth._login_failures.clear()
    try:
        with _enable_auth(monkeypatch, tmp_path) as c:
            c.get("/login")
            tok = c.cookies.get("qf_csrf")
            data = {"username": "admin", "password": "nope", "_csrf_token": tok}
            hdr = {"X-CSRF-Token": tok}
            for _ in range(web_auth._LOGIN_MAX):
                assert c.post("/login", data=data, headers=hdr,
                              follow_redirects=False).status_code == 401
            # One more attempt is throttled rather than just rejected again.
            r = c.post("/login", data=data, headers=hdr, follow_redirects=False)
            assert r.status_code == 429
    finally:
        web_auth._login_failures.clear()


def test_web_auth_none_bypasses_login(monkeypatch, tmp_path):
    # auth off and no credentials configured — every route stays open.
    with _enable_auth(monkeypatch, tmp_path, configure=False) as c:
        monkeypatch.setenv("WEB_AUTH", "none")
        assert c.get("/", follow_redirects=False).status_code == 200


def test_first_run_redirects_to_setup(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path, configure=False) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/setup"


def test_api_endpoint_requires_auth(monkeypatch, tmp_path):
    # The auth gate has to cover the JSON/SSE endpoints, not just page views.
    with _enable_auth(monkeypatch, tmp_path) as c:
        r = c.get("/api/jobs", follow_redirects=False)
        assert r.status_code == 401


def test_malformed_host_cannot_bypass_auth(monkeypatch, tmp_path):
    # CVE-2026-48710: Starlette rebuilds request.url.path from the client Host
    # header, so a host like "example.com/login?x=" used to make the auth
    # middleware read the path as "/login" and wave a protected route through
    # with no session. The gate now reads request.scope["path"] (the real routed
    # path), which a forged Host cannot touch — protected routes stay closed.
    with _enable_auth(monkeypatch, tmp_path) as c:
        bad = {"host": "example.com/login?x="}
        # Page route: redirected to login, never served.
        r = c.get("/settings", headers=bad, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"
        # JSON route: 401, not a 200 leaking state.
        r = c.get("/api/jobs", headers=bad, follow_redirects=False)
        assert r.status_code == 401
        # Write route is unreachable too (never a 200).
        r = c.post("/queue/cancel-pending", headers=bad,
                   follow_redirects=False)
        assert r.status_code != 200


def test_setup_creates_login_and_signs_in(monkeypatch, tmp_path):
    from qobuz_librarian.web import auth as web_auth

    with _enable_auth(monkeypatch, tmp_path, configure=False) as c:
        c.get("/setup")
        tok = c.cookies.get("qf_csrf")
        r = c.post("/setup",
                   data={"username": "admin", "password": "hunter2hunter",
                         "confirm": "hunter2hunter", "_csrf_token": tok},
                   headers={"X-CSRF-Token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert web_auth.credentials_configured()
        # Setup signs the user straight in.
        assert c.get("/", follow_redirects=False).status_code == 200


def test_migrate_page_renders_unconfigured_and_configured(client, monkeypatch):
    import qobuz_librarian.config as cfg
    monkeypatch.setattr(cfg, "MIGRATE_SRC", "")
    monkeypatch.setattr(cfg, "MIGRATE_DEST", "")
    r = client.get("/migrate")
    assert r.status_code == 200
    assert "QL_MIGRATE_SRC" in r.text                 # the configure CTA
    monkeypatch.setattr(cfg, "MIGRATE_SRC", "/some/src")
    monkeypatch.setattr(cfg, "MIGRATE_DEST", "/some/dest")
    r2 = client.get("/migrate")
    assert r2.status_code == 200
    assert "Preview migration" in r2.text             # the start form


def test_migrate_post_without_paths_reports_error_not_500(client, monkeypatch):
    import qobuz_librarian.config as cfg
    monkeypatch.setattr(cfg, "MIGRATE_SRC", "")
    monkeypatch.setattr(cfg, "MIGRATE_DEST", "")
    r = client.post("/migrate", data={}, follow_redirects=False)
    assert r.status_code == 200
    assert "QL_MIGRATE_SRC" in r.text


def test_migrate_post_submits_a_creds_free_job(client, monkeypatch, tmp_path):
    import qobuz_librarian.config as cfg
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(cfg, "MIGRATE_SRC", str(src))
    monkeypatch.setattr(cfg, "MIGRATE_DEST", str(tmp_path / "dest"))
    r = client.post("/migrate", data={"in_place": "on"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/jobs/")
    job_id = r.headers["location"].split("/jobs/")[1].split("?")[0]
    job = jm.registry.get(job_id)
    assert job is not None
    assert job.review_verb == "Move"                  # in-place toggle carried through
    _remove_job(job)


def test_settings_path_resolver_maps_container_paths_to_host_bind_mounts(
    monkeypatch, tmp_path
):
    from qobuz_librarian.web.app import _resolve_host_path

    fake_mountinfo = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 0:2 /home/me/music /music rw - ext4 /dev/sda1 rw\n"
        "3 1 0:3 /home/me/stack/config /config rw - ext4 /dev/sda1 rw\n"
    )
    fake = tmp_path / "mountinfo"
    fake.write_text(fake_mountinfo)
    import builtins
    real_open = builtins.open
    def patched_open(path, *a, **kw):
        if path == "/proc/self/mountinfo":
            return real_open(fake, *a, **kw)
        return real_open(path, *a, **kw)
    monkeypatch.setattr(builtins, "open", patched_open)

    assert _resolve_host_path("/music") == ("/home/me/music", True)
    assert _resolve_host_path("/config/beets/musiclibrary.db") == (
        "/home/me/stack/config/beets/musiclibrary.db", True)
    assert _resolve_host_path("/anonymous-volume") == ("/anonymous-volume", False)
    from pathlib import Path
    assert _resolve_host_path(Path("/music")) == ("/home/me/music", True)


def test_prune_keeps_a_finished_job_that_is_still_being_streamed(monkeypatch):
    reg = jm.JobRegistry()
    monkeypatch.setattr(reg, "MAX_FINISHED", 2)
    oldest = jm.Job(title="streamed", status=jm.JobStatus.DONE)
    reg.add(oldest)
    reg.add(jm.Job(title="old2", status=jm.JobStatus.DONE))
    oldest.subscribe()                       # a client is watching the oldest job
    for i in range(3):                       # push well past MAX_FINISHED
        reg.add(jm.Job(title=f"new{i}", status=jm.JobStatus.DONE))
    assert reg.get(oldest.id) is not None    # not yanked out from under the stream


def test_session_tokens_are_per_login_and_revocable():
    from qobuz_librarian.web import auth as web_auth
    web_auth.revoke_all_sessions()
    t1 = web_auth.mint_session()
    t2 = web_auth.mint_session()
    assert t1 != t2                              # per-login, not one shared secret
    assert web_auth.verify_session(t1) and web_auth.verify_session(t2)
    web_auth.revoke_session(t1)                  # logout of one browser
    assert not web_auth.verify_session(t1)       # ...that session is dead...
    assert web_auth.verify_session(t2)           # ...the other still works
    web_auth.revoke_all_sessions()               # e.g. on a password change
    assert not web_auth.verify_session(t2)
    assert web_auth.verify_session("") is False


def test_per_account_throttle_survives_ip_rotation(monkeypatch):
    from qobuz_librarian.web import auth as web_auth
    monkeypatch.setattr(web_auth, "_login_failures", {})
    monkeypatch.setattr(web_auth, "_user_failures", {})
    monkeypatch.setattr(web_auth, "_USER_LOGIN_MAX", 3)
    # Same account, a fresh source IP each attempt — the per-IP counter never
    # trips, so only the per-account counter can stop the rotation attack.
    for i in range(3):
        assert web_auth.check_login_rate_limit(f"10.0.0.{i}", "admin") is True
        web_auth.record_login_failure(f"10.0.0.{i}", "admin")
    assert web_auth.check_login_rate_limit("10.0.0.99", "admin") is False
    # A different account from a clean IP is unaffected.
    assert web_auth.check_login_rate_limit("10.0.0.99", "someoneelse") is True
    # A successful login clears the account's failures and lifts the block.
    web_auth.clear_login_failures("10.0.0.99", "admin")
    assert web_auth.check_login_rate_limit("10.0.0.100", "admin") is True
