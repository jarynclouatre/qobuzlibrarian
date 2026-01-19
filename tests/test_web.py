"""Tests for the web UI: background job system (jobs.py) and HTTP routes (app.py)."""
import queue
import time

import pytest

from qobuz_fetch.web import jobs as jm

# ── jobs.py: Job ──────────────────────────────────────────────────────────────

def test_push_line_appends_and_notifies_subscriber():
    job = jm.Job(title="X")
    sub = job.subscribe()
    job.push_line("hello")
    assert job.log_lines == ["hello"]
    assert sub.get_nowait() == "hello"


def test_log_lines_capped_with_truncation_marker():
    job = jm.Job()
    total = jm.Job.LOG_CAP + jm.Job._LOG_SLACK + 1
    for i in range(total):
        job.push_line(f"line{i}")
    assert len(job.log_lines) == jm.Job.LOG_CAP
    assert job.log_lines[0] == jm.Job._TRUNCATION_MARKER
    assert job.log_lines[-1] == f"line{total - 1}"


def test_log_lines_not_trimmed_below_threshold():
    job = jm.Job()
    for i in range(jm.Job.LOG_CAP):
        job.push_line(f"l{i}")
    assert len(job.log_lines) == jm.Job.LOG_CAP
    assert job.log_lines[0] == "l0"  # no trim, no marker


def test_push_line_strips_control_bytes():
    """NUL/ESC/BEL in worker output would truncate some browsers' SSE display
    and garble the JSON status endpoint — strip them at intake."""
    job = jm.Job()
    job.push_line("hello\x00world\x07\x1bend")
    assert job.log_lines == ["helloworldend"]
    # \t and \n are preserved (tabs in subprocess output, newlines re-emitted).
    job.push_line("a\tb\nc")
    assert job.log_lines[-1] == "a\tb\nc"


# ── jobs.py: JobRegistry ──────────────────────────────────────────────────────

def test_registry_add_get_all_preserves_order():
    reg = jm.JobRegistry()
    j1, j2 = jm.Job(title="1"), jm.Job(title="2")
    reg.add(j1)
    reg.add(j2)
    assert reg.get(j1.id) is j1
    assert [j.id for j in reg.all()] == [j1.id, j2.id]


def test_registry_partitions_pending_and_finished():
    reg = jm.JobRegistry()
    pending = jm.Job(status=jm.JobStatus.PENDING)
    running = jm.Job(status=jm.JobStatus.RUNNING)
    done = jm.Job(status=jm.JobStatus.DONE)
    failed = jm.Job(status=jm.JobStatus.FAILED)
    for j in (pending, running, done, failed):
        reg.add(j)
    assert {j.id for j in reg.pending_and_running()} == {pending.id, running.id}
    assert {j.id for j in reg.finished()} == {done.id, failed.id}


# ── jobs.py: JobLogHandler ────────────────────────────────────────────────────

def test_log_handler_strips_ansi_and_pushes():
    import logging
    job = jm.Job()
    handler = jm.JobLogHandler(job)
    handler.setFormatter(logging.Formatter("%(message)s"))
    rec = logging.LogRecord("x", logging.INFO, "f", 1,
                            "\x1b[32mcolored\x1b[0m text", None, None)
    handler.emit(rec)
    assert job.log_lines == ["colored text"]


def test_log_handler_swallows_push_line_failure():
    import logging
    job = jm.Job()
    handler = jm.JobLogHandler(job)
    handler.setFormatter(logging.Formatter("%(message)s"))

    def boom(_line):
        raise RuntimeError("push failed")
    job.push_line = boom
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
    handler.emit(rec)


# ── jobs.py: worker loop ──────────────────────────────────────────────────────

def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_submit_runs_job_to_done():
    jm.start_worker()
    ran = []
    job = jm.Job(title="ok")
    sub = job.subscribe()
    jm.submit(job, lambda j: ran.append(j.id))
    assert _wait_for(lambda: job.status == jm.JobStatus.DONE)
    assert ran == [job.id]
    # Stream-end marker goes to live subscribers only — not into log_lines.
    assert jm.STREAM_END not in job.log_lines
    drained = []
    while not sub.empty():
        drained.append(sub.get_nowait())
    assert jm.STREAM_END in drained
    assert job.finished_at is not None


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


def test_submit_failing_job_marks_failed():
    jm.start_worker()

    def boom(_j):
        raise RuntimeError("kaboom")

    job = jm.Job(title="bad")
    jm.submit(job, boom)
    assert _wait_for(lambda: job.status == jm.JobStatus.FAILED)
    assert job.error == "kaboom"
    assert any("kaboom" in ln for ln in job.log_lines)


def test_approve_enqueues_before_flipping_status(monkeypatch):
    """approve() must put the job on the work queue BEFORE marking it
    PENDING. The reverse order can orphan the job in PENDING with nothing
    queued if the process dies between the two statements."""
    job = jm.Job(title="scan-approve")
    job.kind = "scan"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda j, chosen: None
    job.add_candidate("album", "A", "Artist", payload={"id": 1})

    seen_status_at_put = []
    orig_put = jm._work_queue.put

    def _spy_put(item):
        seen_status_at_put.append(item[0].status)
        orig_put(item)

    monkeypatch.setattr(jm._work_queue, "put", _spy_put)

    assert jm.approve(job, ["c1"])
    assert seen_status_at_put == [jm.JobStatus.AWAITING_REVIEW]


def test_base_exception_in_job_does_not_kill_worker():
    """Worker thread must survive BaseException — otherwise every subsequent
    job hangs forever with no error."""
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

    from qobuz_fetch.web.app import app
    with TestClient(app) as c:
        c.get("/")
        token = c.cookies.get("qf_csrf")
        c.headers.update({"X-CSRF-Token": token})
        yield c


def test_dashboard_ok(client):
    r = client.get("/")
    assert r.status_code == 200


def test_search_empty_query_has_no_error(client):
    r = client.post("/search", data={"q": "   "})
    assert r.status_code == 200
    assert "alert-error" not in r.text


def test_job_page_unknown_redirects_to_queue(client):
    r = client.get("/jobs/nope")
    assert r.status_code == 200
    assert "Download queue" in r.text


def test_download_error_raw_exception_not_reflected(client, monkeypatch):
    """Unexpected errors must not be reflected verbatim in the response.
    The user sees a generic message; the raw exception text is suppressed."""
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")

    def boom(_id, _token):
        raise RuntimeError("<script>alert(1)</script>")

    monkeypatch.setattr(search_mod, "get_album", boom)
    r = client.post("/download", data={"album_id": "x"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "alert-error" in r.text


def test_download_authlost_branch_redirects_to_settings_error(client, monkeypatch):
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.web.app as app_mod
    from qobuz_fetch.api.auth import AuthLost
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
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.web.app as app_mod
    from qobuz_fetch.api.auth import QobuzError
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")

    def qberr(_id, _token):
        raise QobuzError("HTTP 500 from album/get: <html>internal sentinel xyz</html>")
    monkeypatch.setattr(search_mod, "get_album", qberr)
    r = client.post("/download", data={"album_id": "x"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "internal sentinel xyz" not in r.text
    assert "alert-error" in r.text


def test_search_result_id_is_html_escaped(client, monkeypatch):
    """The hidden album_id input must HTML-escape its value so a quote
    can't break out of the attribute and inject extra form fields."""
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.library.catalog as cat
    import qobuz_fetch.web.app as webapp
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


def test_hx_confirm_uses_single_quoted_outer_attribute(client, monkeypatch):
    """hx-confirm must use a single-quoted attribute so inner double-quotes
    around the album title don't close it early."""
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.library.catalog as cat
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "search_albums",
                         lambda q, t, limit=None: [{
                             "id": "123", "title": "Ode to Joy",
                             "artist": {"name": "Beethoven"},
                             "tracks_count": 9, "maximum_bit_depth": 24}])
    monkeypatch.setattr(cat, "album_quality_label", lambda a: "Hi-Res")
    monkeypatch.setattr(cat, "album_year", lambda a: 1824)
    r = client.post("/search", data={"q": "beethoven"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Ode to Joy" in r.text
    assert "hx-confirm=" in r.text
    assert 'hx-confirm="Download "' not in r.text


def test_search_query_prefills_input_on_full_page_render(client, monkeypatch):
    """Full-page search POST must pre-fill the input with the searched value."""
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.library.catalog as cat
    import qobuz_fetch.web.app as webapp

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    monkeypatch.setattr(search_mod, "search_albums", lambda q, t, limit=None: [])
    monkeypatch.setattr(cat, "album_quality_label", lambda a: "x")
    monkeypatch.setattr(cat, "album_year", lambda a: 2020)

    r = client.post("/search", data={"q": "beethoven"})
    assert r.status_code == 200
    assert 'value="beethoven"' in r.text


def test_search_get_with_query_runs_search_and_prefills_input(client, monkeypatch):
    """GET /search?q=beethoven must execute the search and pre-fill the box."""
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    called_with = []

    def _fake_search(q, tok, limit=None):
        called_with.append(q)
        return []
    monkeypatch.setattr(search_mod, "search_albums", _fake_search)
    r = client.get("/search?q=beethoven")
    assert r.status_code == 200
    assert 'value="beethoven"' in r.text
    assert called_with == ["beethoven"]


def test_search_artist_url_shows_helpful_error(client, monkeypatch):
    """Artist/interpreter Qobuz URLs must show a helpful error, not empty results."""
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")

    r = client.post("/search",
                    data={"q": "https://www.qobuz.com/us-en/interpreter/test/123"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Only Qobuz album URLs are supported here" in r.text
    assert "No results" not in r.text


def test_search_treats_lookalike_url_as_text(client, monkeypatch):
    """A free-text query containing 'qobuz.com' but with a non-qobuz host
    must NOT be routed through the URL paths."""
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "search_albums",
                        lambda q, t, limit=None: [])
    r = client.post("/search",
                    data={"q": "https://evil.example.com/qobuz.com/album/x"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Only Qobuz album URLs are supported here" not in r.text


def test_queue_renders_job_without_artist_without_none(client):
    job = _inject_job(jm.JobStatus.PENDING, title="No-artist job")
    job.artist = None
    try:
        r = client.get("/queue")
        assert r.status_code == 200
        # The job title is rendered; "None" must not leak into the markup.
        assert "No-artist job" in r.text
        assert "None" not in r.text.split("</style>")[-1]
    finally:
        _remove_job(job)


def test_dashboard_renders_job_without_artist_without_none(client):
    job = _inject_job(jm.JobStatus.PENDING, title="No-artist job")
    job.artist = None
    try:
        r = client.get("/")
        assert r.status_code == 200
        # Just verify the dashboard renders without ">None<" leaking
        # through a missing-artist guard.
        assert ">None<" not in r.text
    finally:
        _remove_job(job)


def test_dashboard_no_creds_shows_setup_cta(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {})
    r = client.get("/")
    assert r.status_code == 200
    assert "Set up your Qobuz account" in r.text


def test_csrf_oversize_body_rejected_before_parse(client):
    """A POST whose content-length exceeds the cap must return 413
    without parsing or buffering the body."""
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
    """An in-flight job must not see cfg.* flip mid-run. save() persists
    to disk immediately but defers the in-memory apply until drain_pending."""
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)
    monkeypatch.setattr(ss, "_any_active_job", lambda: True)
    with ss._pending_lock:
        ss._pending_apply = None

    ok = ss.save({"AUTO_UPGRADE_ENABLED": True})
    assert ok is True
    assert (tmp_path / "s.json").exists()
    assert cfg.AUTO_UPGRADE_ENABLED is False  # not yet applied

    ss.drain_pending()
    assert cfg.AUTO_UPGRADE_ENABLED is True
    ss.drain_pending()
    assert cfg.AUTO_UPGRADE_ENABLED is True  # idempotent


def test_current_overlays_pending_apply(monkeypatch):
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import settings_store as ss

    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)
    try:
        with ss._pending_lock:
            ss._pending_apply = {"AUTO_UPGRADE_ENABLED": True}
        out = ss.current()
        assert out["AUTO_UPGRADE_ENABLED"] is True
        assert cfg.AUTO_UPGRADE_ENABLED is False
    finally:
        with ss._pending_lock:
            ss._pending_apply = None


def test_drain_pending_lock_spans_apply_so_concurrent_save_keeps_both(
        tmp_path, monkeypatch):
    """A save() and a drain_pending() racing against each other must both
    land their changes; neither overwrites the other's pending_apply."""
    import threading

    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)
    monkeypatch.setattr(cfg, "COMPRESS_ENABLED", False)

    with ss._pending_lock:
        ss._pending_apply = None

    # First save defers (active job).
    monkeypatch.setattr(ss, "_any_active_job", lambda: True)
    assert ss.save({"AUTO_UPGRADE_ENABLED": True}) is True

    inside_apply = threading.Event()
    release_apply = threading.Event()

    real_apply = ss._apply

    def slow_apply(values):
        inside_apply.set()
        release_apply.wait(timeout=2)
        real_apply(values)

    monkeypatch.setattr(ss, "_apply", slow_apply)

    def run_drain():
        ss.drain_pending()

    drain_thread = threading.Thread(target=run_drain)
    drain_thread.start()
    assert inside_apply.wait(timeout=2)

    # Concurrent save arrives while drain is mid-apply. Pretend no active
    # job so this save tries to _apply immediately.
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)

    save_thread = threading.Thread(
        target=lambda: ss.save({"COMPRESS_ENABLED": True}))
    save_thread.start()

    release_apply.set()
    drain_thread.join(timeout=2)
    save_thread.join(timeout=2)

    assert cfg.AUTO_UPGRADE_ENABLED is True
    assert cfg.COMPRESS_ENABLED is True


def test_execute_upgrades_does_not_flip_global_cfg(monkeypatch):
    """An upgrade run must enable its own auto_upgrade path via args, not
    flip cfg.AUTO_UPGRADE_ENABLED — the Settings page reads cfg and would
    momentarily show True if it had been mutated mid-job."""
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import flows

    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)
    seen_args = []

    def fake_process_album(album, args, **kwargs):
        seen_args.append(getattr(args, "auto_upgrade", None))
        # While the run is in flight, cfg.AUTO_UPGRADE_ENABLED must NOT
        # have been flipped to True.
        assert cfg.AUTO_UPGRADE_ENABLED is False
        return {"imported": False, "result": "user_skipped"}

    import qobuz_fetch.modes.process as proc_mod
    monkeypatch.setattr(proc_mod, "process_album", fake_process_album)

    class _FakeJob:
        cancel_requested = False
    chosen = [{
        "artist": "Artist",
        "payload": {"candidate": {"qobuz_album": {"id": "1", "title": "A"}}},
    }]
    monkeypatch.setattr(cfg, "ARTIST_API_DELAY", 0.0)
    flows.execute_upgrades(_FakeJob(), chosen, token="tok")

    assert seen_args == [True]
    assert cfg.AUTO_UPGRADE_ENABLED is False


# ── CSRF middleware ───────────────────────────────────────────────────────────

def test_csrf_post_without_token_is_rejected():
    """One representative POST verifies CSRF-missing → 403. The middleware
    is mounted at the app level, so per-route coverage is unnecessary."""
    from fastapi.testclient import TestClient

    from qobuz_fetch.web.app import app
    with TestClient(app) as c:
        c.get("/")
        r = c.post("/search", data={"q": "anything"})
        assert r.status_code == 403


def test_csrf_cookie_has_samesite_strict():
    from fastapi.testclient import TestClient

    from qobuz_fetch.web.app import app
    with TestClient(app) as c:
        r = c.get("/")
        set_cookie = r.headers.get("set-cookie", "")
        assert "qf_csrf=" in set_cookie
        assert "samesite=strict" in set_cookie.lower()


def test_csrf_form_field_body_replayed_to_route():
    """CSRF token in the POST body must not consume the body before FastAPI
    reads Form(...) params from it."""
    from fastapi.testclient import TestClient

    from qobuz_fetch.web.app import app
    from qobuz_fetch.web.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD
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
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import app as webapp
    cfg_path = tmp_path / "streamrip" / "config.toml"
    monkeypatch.setattr(cfg, "STREAMRIP_CONFIG", cfg_path)
    monkeypatch.setattr(cfg, "QOBUZ_USER_AUTH_TOKEN", "")
    webapp._write_creds("user42", "secret-token")
    assert cfg_path.exists()
    creds = webapp._read_creds()
    assert creds == {"user_id": "user42", "auth_token": "secret-token"}


# ── run-lock busy → destructive routes 503, read-only stay open ───────

def test_lock_busy_refuses_destructive_routes(monkeypatch):
    """When the web process couldn't acquire the run-lock at startup,
    staging-writing POSTs must 503; read-only pages stay reachable."""
    from fastapi.testclient import TestClient

    from qobuz_fetch.web import app as webapp
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
    from qobuz_fetch.web import app as webapp
    from qobuz_fetch.web import flows
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(flows, "run_lyric_retry", lambda j, t: None)
    r = client.post("/lyric-retry", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/jobs/")


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

        from qobuz_fetch.web import flows

        monkeypatch.setattr(flows, "get_album",
                            lambda aid, tok: {"id": aid, "title": "T",
                                              "tracks": {"items": []}})
        monkeypatch.setattr("qobuz_fetch.modes.process.process_album",
                            lambda *a, **k: {
                                "result": "downloaded", "n_ok": 5,
                                "n_fail": 0, "n_lossy": 0,
                                "imported": False, "auto_upgrade": False})
        monkeypatch.setattr("qobuz_fetch.config.ARTIST_API_DELAY", 0)

        with caplog.at_level(logging.INFO, logger="qobuz_librarian"):
            flows.execute_albums(jm.Job(title="t"), [self._candidate()], "tok")
        assert any("0/1" in r.message for r in caplog.records)

    def test_execute_upgrades_not_counted_when_import_fails(self,
                                                             monkeypatch, caplog):
        """Beets failure must not count as an upgrade."""
        import logging

        from qobuz_fetch.web import flows

        monkeypatch.setattr("qobuz_fetch.modes.process.process_album",
                            lambda *a, **k: {
                                "result": "downloaded", "n_ok": 5,
                                "n_fail": 0, "n_lossy": 0,
                                "imported": False, "auto_upgrade": True})
        monkeypatch.setattr("qobuz_fetch.config.ARTIST_API_DELAY", 0)
        monkeypatch.setattr("qobuz_fetch.config.AUTO_UPGRADE_ENABLED", False)

        cand = {
            "payload": {"candidate": {
                "qobuz_album": {"id": "A1", "title": "T"},
            }},
            "title": "Album", "artist": "Artist",
        }
        with caplog.at_level(logging.INFO, logger="qobuz_librarian"):
            flows.execute_upgrades(jm.Job(title="t"), [cand], "tok")
        assert any("0/1" in r.message for r in caplog.records)


# ── Settings diagnostics card ────────────────────────────────────────

def test_diagnostics_reports_paths_and_binaries(tmp_path, monkeypatch):
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import app as webapp
    music = tmp_path / "music"
    music.mkdir()
    (music / "Some Artist").mkdir()
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "not_mounted")

    checks = webapp._diagnostics()
    by_label = {c["label"]: c for c in checks}

    assert by_label["Music library (MUSIC_ROOT)"]["ok"] is True
    assert by_label["Staging (STAGING_DIR)"]["ok"] is False
    assert "`rip` binary" in by_label
    assert "`beet` binary" in by_label
    assert "`ffprobe` binary" in by_label


def test_diagnostics_reports_missing_binary_as_failure(tmp_path, monkeypatch):
    import shutil

    from qobuz_fetch.web import app as webapp
    monkeypatch.setattr(shutil, "which", lambda _b: None)
    checks = webapp._diagnostics()
    binary_rows = [c for c in checks if "binary" in c["label"]]
    assert binary_rows, "diagnostics should still include binary rows"
    assert all(c["ok"] is False for c in binary_rows)


def test_diagnostics_beets_db_existing_readable(tmp_path, monkeypatch):
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import app as webapp
    db = tmp_path / "library.db"
    db.touch()
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", db)
    checks = webapp._diagnostics()
    row = next(c for c in checks if c["label"] == "beets DB (BEETS_DB_PATH)")
    assert row["ok"] is True
    assert str(db) in row["detail"]


def test_diagnostics_beets_db_not_yet_created(tmp_path, monkeypatch):
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import app as webapp
    db = tmp_path / "library.db"
    # Parent exists, file doesn't yet.
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", db)
    checks = webapp._diagnostics()
    row = next(c for c in checks if c["label"] == "beets DB (BEETS_DB_PATH)")
    assert row["ok"] is True
    assert "first import" in row["detail"]


def test_diagnostics_beets_db_parent_missing(tmp_path, monkeypatch):
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import app as webapp
    db = tmp_path / "nope" / "library.db"
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", db)
    checks = webapp._diagnostics()
    row = next(c for c in checks if c["label"] == "beets DB (BEETS_DB_PATH)")
    assert row["ok"] is False
    assert "does not exist" in row["detail"]


def test_settings_page_renders_without_error(client):
    """GET /settings must render without 500 — template uses behavior[key]
    with bracket notation; if current() is missing any key it raises UndefinedError."""
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Settings" in r.text
    assert "Qobuz authentication" in r.text
    assert "Diagnostics" in r.text


def test_settings_save_with_env_creds_and_blank_form_succeeds(client, monkeypatch):
    monkeypatch.setenv("QOBUZ_USER_AUTH_TOKEN", "env-tok")
    # Patch _write_creds to return False so any accidental call would surface
    # as an error redirect — proving the env-creds path skips the write.
    import qobuz_fetch.web.app as _app
    monkeypatch.setattr(_app, "_write_creds", lambda *_: False)
    r = client.post("/settings", data={"user_id": "", "auth_token": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "saved=1" in r.headers["location"]


def test_settings_empty_save_with_no_existing_creds_returns_error(client, monkeypatch):
    """An empty submission on first-run must surface a warning, not flash a
    green 'Settings saved' banner while the dashboard still shows the CTA."""
    import qobuz_fetch.web.app as _app
    monkeypatch.delenv("QOBUZ_USER_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(_app, "_read_creds", lambda: {})
    r = client.post("/settings", data={"user_id": "", "auth_token": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?error=empty"


def test_base_template_has_nav_landmark(client):
    """The accessibility landmark must be a <nav> element so screen readers
    announce it as navigation, not a generic block."""
    r = client.get("/")
    assert "<nav " in r.text


def test_streamrip_quality_stays_int_after_settings_save(monkeypatch):
    """cfg.STREAMRIP_QUALITY is loaded as int from env; the Settings enum
    posts strings ("4"), but _apply must coerce back to int so
    streamrip_quality_cap() doesn't see a str."""
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import settings_store
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    settings_store._apply({"STREAMRIP_QUALITY": "3"})
    assert cfg.STREAMRIP_QUALITY == 3
    assert isinstance(cfg.STREAMRIP_QUALITY, int)


def test_settings_save_creds_write_failure_redirects_with_error(client, monkeypatch):
    import qobuz_fetch.web.app as _app
    monkeypatch.delenv("QOBUZ_USER_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(_app, "_write_creds", lambda *_: False)
    r = client.post("/settings", data={"user_id": "u", "auth_token": "t"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=creds" in r.headers["location"]


def test_settings_save_with_bad_token_redirects_with_auth_warning(client, monkeypatch):
    """When the saved token doesn't authenticate, the redirect carries
    auth=bad so the settings page surfaces a warning instead of green."""
    import qobuz_fetch.api.client as client_mod
    import qobuz_fetch.web.app as _app
    from qobuz_fetch.api.auth import AuthLost
    monkeypatch.delenv("QOBUZ_USER_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(_app, "_write_creds", lambda *_: True)

    def reject(*a, **k):
        raise AuthLost("invalid token")
    monkeypatch.setattr(client_mod, "qobuz_get", reject)
    r = client.post("/settings", data={"user_id": "u", "auth_token": "tok"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "saved=1" in loc and "auth=bad" in loc


def test_settings_save_with_good_token_redirects_clean(client, monkeypatch):
    """A token that authenticates against Qobuz doesn't trigger the warning."""
    import qobuz_fetch.api.client as client_mod
    import qobuz_fetch.web.app as _app
    monkeypatch.delenv("QOBUZ_USER_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(_app, "_write_creds", lambda *_: True)
    monkeypatch.setattr(client_mod, "qobuz_get", lambda *a, **k: {"ok": 1})
    r = client.post("/settings", data={"user_id": "u", "auth_token": "tok"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "saved=1" in loc and "auth=bad" not in loc


def test_settings_behavior_persist_failure_redirects_with_error(client, monkeypatch):
    from qobuz_fetch.web import settings_store
    monkeypatch.setattr(settings_store, "save", lambda *_: False)
    r = client.post("/settings/behavior", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert "error=persist" in r.headers["location"]


def test_settings_behavior_save_with_active_job_signals_queued(client, monkeypatch):
    from qobuz_fetch.web import settings_store
    monkeypatch.setattr(settings_store, "save", lambda *_: True)
    monkeypatch.setattr(settings_store, "_any_active_job", lambda: True)
    r = client.post("/settings/behavior", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert "queued=1" in r.headers["location"]
    follow = client.get(r.headers["location"])
    assert follow.status_code == 200
    assert "after the current job" in follow.text


def test_queue_clear_redirects_to_queue(client):
    """POST /queue/clear must drop finished jobs and redirect to /queue."""
    job = _inject_job(jm.JobStatus.DONE, title="Old Job")
    try:
        r = client.post("/queue/clear", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].endswith("/queue")
    finally:
        _remove_job(job)


def test_security_headers_present(client):
    """Security headers must be present on every response."""
    r = client.get("/")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "same-origin"
    csp = r.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_hsts_header_present_on_https_but_not_http(client):
    """HSTS must only ship on HTTPS — sending it over plain HTTP can lock
    a user out if they later reach the host via HTTP after an HTTPS visit."""
    https = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=" in https.headers.get("Strict-Transport-Security", "")
    plain = client.get("/")
    assert "Strict-Transport-Security" not in plain.headers


def test_web_fetch_timeout_honors_env_var(monkeypatch):
    """The web fetch timeout must come from QF_WEB_FETCH_TIMEOUT so an
    operator can shrink/grow it without editing source."""
    import importlib

    import qobuz_fetch.config as cfg_mod
    monkeypatch.setenv("QF_WEB_FETCH_TIMEOUT", "0.001")
    monkeypatch.setenv("QF_WEB_TEST_AUTH_TIMEOUT", "0.002")
    reloaded = importlib.reload(cfg_mod)
    try:
        assert reloaded.WEB_FETCH_TIMEOUT == 0.001
        assert reloaded.WEB_TEST_AUTH_TIMEOUT == 0.002
    finally:
        importlib.reload(cfg_mod)


# ── queue_download: job must be FAILED when process_album fails ───────────────

def _download_job(client, monkeypatch, album_id, process_result):
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.web.app as webapp

    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Album",
                                          "artist": {"name": "Artist"}})
    monkeypatch.setattr("qobuz_fetch.modes.process.process_album",
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


def test_process_album_returns_dict_for_lossy_album():
    """Contract test pinning the boundary `_download_job` stubs out:
    process_album must return a dict containing a `result` key."""
    from argparse import Namespace

    from qobuz_fetch.modes.process import process_album
    lossy_album = {"id": "lossy", "title": "T",
                   "artist": {"name": "A"},
                   "maximum_bit_depth": 0, "tracks": {"items": []}}
    args = Namespace(force=False, dry_run=False, yes=True, verbose=False)
    result = process_album(lossy_album, args, token="tok")
    assert isinstance(result, dict)
    assert "result" in result
    assert result["result"] == "lossy_only"


def test_process_album_returns_dict_for_album_with_no_tracks(monkeypatch):
    from argparse import Namespace

    from qobuz_fetch.modes import process as process_mod
    monkeypatch.setattr(process_mod, "is_lossless_album", lambda a: True)
    empty_album = {"id": "x", "title": "T", "artist": {"name": "A"},
                   "tracks": {"items": []}}
    args = Namespace(force=False, dry_run=False, yes=True, verbose=False)
    result = process_mod.process_album(empty_album, args, token="tok")
    assert isinstance(result, dict)
    assert result["result"] == "no_tracks"


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

def test_active_job_includes_scanning_status():
    """Dashboard active_job indicator must fire during SCANNING, not only RUNNING."""
    from qobuz_fetch.web.app import _active_job

    job = jm.Job(title="scan-job")
    job.status = jm.JobStatus.SCANNING
    jm.registry.add(job)

    try:
        found = _active_job()
        assert found is not None and found.id == job.id
    finally:
        with jm.registry._lock:
            jm.registry._jobs.pop(job.id, None)
            try:
                jm.registry._order.remove(job.id)
            except ValueError:
                pass


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


def test_queue_pending_job_shows_cancel_button(client):
    """A pending job must have a × cancel form on the queue page."""
    job = _inject_job(jm.JobStatus.PENDING)
    try:
        r = client.get("/queue")
        assert r.status_code == 200
        assert f"/jobs/{job.id}/cancel" in r.text
    finally:
        _remove_job(job)


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


def test_cancel_pending_job_redirects_to_job_page(client):
    """POSTing cancel on a pending job returns a redirect."""
    job = _inject_job(jm.JobStatus.PENDING)
    try:
        r = client.post(f"/jobs/{job.id}/cancel", follow_redirects=False)
        assert r.status_code == 303
    finally:
        _remove_job(job)


# done-event shows a banner instead of auto-reloading (browser-only).
# The JS change is verified by checking the template no longer contains
# location.reload() in the live SSE handler path.
def test_job_page_done_handler_does_not_auto_reload():
    """job.html done handler must show a banner, not call location.reload()."""
    import re
    tmpl = (
        __import__("pathlib").Path(__file__).parent.parent
        / "src/qobuz_fetch/web/templates/job.html"
    ).read_text()
    # The only location.reload() left should be inside the banner button's onclick.
    reloads = re.findall(r"location\.reload\(\)", tmpl)
    assert len(reloads) == 1, "expected exactly one location.reload() in banner onclick"


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
    assert r2.json()["error"] == "not found"


def test_post_job_hook_receives_terminal_state_as_json(tmp_path, monkeypatch):
    import json

    from qobuz_fetch.web import jobs as _jobs
    out = tmp_path / "hook.log"
    monkeypatch.setenv("POST_JOB_HOOK", f"cat > {out}")

    job = jm.Job(title="HookJob", artist="A")
    job.id = "hook-test-id"
    job.status = jm.JobStatus.DONE
    job.finished_at = 1234.0
    _jobs._fire_post_job_hook(job)
    # The subprocess.Popen runs async, but communicate() blocks; the file
    # should be written by the time _fire_post_job_hook returns.
    import time as _t
    for _ in range(20):
        if out.exists() and out.read_text():
            break
        _t.sleep(0.05)
    payload = json.loads(out.read_text())
    assert payload["id"] == "hook-test-id"
    assert payload["status"] == "done"
    assert payload["title"] == "HookJob"


def test_post_job_hook_unset_is_no_op(monkeypatch):
    from qobuz_fetch.web import jobs as _jobs
    monkeypatch.delenv("POST_JOB_HOOK", raising=False)
    job = jm.Job(title="x")
    _jobs._fire_post_job_hook(job)  # must not raise


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
    finally:
        _remove_job(pending)
        _remove_job(done)


def test_dashboard_null_fetch_log_fields_render_safe(client, monkeypatch):
    import qobuz_fetch.ui_cli.prompts as prompts_mod
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


def test_dashboard_null_result_field_renders_safe(client, monkeypatch):
    import qobuz_fetch.ui_cli.prompts as prompts_mod
    monkeypatch.setattr(
        prompts_mod,
        "_read_fetch_log",
        lambda **kw: [{"ts": "2025-01-01T00:00:00", "artist": "X", "title": "Y",
                       "result": None, "tracks_downloaded": 0}],
    )
    r = client.get("/")
    assert r.status_code == 200


def test_library_scan_cancel_during_walk_ends_canceled():
    """A scan_fn that sets cancel_requested and returns early must end CANCELED."""
    captured = []

    original_put = jm._work_queue.put
    jm._work_queue.put = captured.append
    try:
        job = jm.Job(title="Library gap scan")

        def cancel_mid_walk(j):
            j.cancel_requested = True

        jm.submit_scan(job, cancel_mid_walk, lambda j, chosen: None)
    finally:
        jm._work_queue.put = original_put

    assert len(captured) == 1
    _, fn = captured[0]
    jm._run_task(job, fn)
    assert job.status == jm.JobStatus.CANCELED


# ── /api/test-auth coverage ────────────────────────────────────────

def test_test_auth_success(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr("qobuz_fetch.api.client.qobuz_get", lambda *a, **k: {})
    r = client.post("/api/test-auth", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "alert-success" in r.text
    assert "valid" in r.text


def test_test_auth_no_creds(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    def _no_creds():
        raise SystemExit("no creds")
    monkeypatch.setattr(webapp, "_get_token", _no_creds)
    r = client.post("/api/test-auth", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "alert-error" in r.text
    assert "No Qobuz credentials" in r.text


def test_test_auth_authlost(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    from qobuz_fetch.api.auth import AuthLost
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    def _authlost(*a, **k):
        raise AuthLost("401")
    monkeypatch.setattr("qobuz_fetch.api.client.qobuz_get", _authlost)
    r = client.post("/api/test-auth", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "alert-error" in r.text
    assert "expired or invalid" in r.text


def test_test_auth_network_error(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    from qobuz_fetch.api.auth import QobuzError
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    def _network_err(*a, **k):
        raise QobuzError("connection refused")
    monkeypatch.setattr("qobuz_fetch.api.client.qobuz_get", _network_err)
    r = client.post("/api/test-auth", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "alert-error" in r.text
    assert "network" in r.text.lower()


def test_test_auth_timeout(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    import asyncio
    async def _timeout(*a, **k):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(asyncio, "wait_for", _timeout)
    r = client.post("/api/test-auth", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "alert-error" in r.text
    assert "Timed out" in r.text


def test_test_auth_unexpected_exception_renders_safe_message(client, monkeypatch):
    """Any non-{Auth,Qobuz,Timeout,SystemExit,NoCredsError} exception must
    produce a generic error fragment, not echo the raw repr."""
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    import asyncio

    async def _boom(*a, **k):
        raise RuntimeError("internal sentinel xyz123")
    monkeypatch.setattr(asyncio, "wait_for", _boom)
    r = client.post("/api/test-auth", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "internal sentinel" not in r.text
    assert "alert-error" in r.text


# ── /download duplicate-job rejection ───────────────────────────────

def _inject_album_job(album_id, status=jm.JobStatus.PENDING):
    job = jm.Job(title="T", album_id=album_id, status=status)
    jm.registry.add(job)
    return job


def test_download_duplicate_rejected_htmx(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    existing = _inject_album_job("dup-album")
    r = client.post("/download", data={"album_id": "dup-album"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Already queued" in r.text
    assert existing.id in r.text


def test_download_duplicate_rejected_redirects(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    existing = _inject_album_job("dup-album-2", status=jm.JobStatus.RUNNING)
    r = client.post("/download", data={"album_id": "dup-album-2"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert existing.id in r.headers["location"]


def test_download_duplicate_matches_scan_candidate(client, monkeypatch):
    """A scan-flow job awaiting review with album X among candidates must
    block a fresh /download of X, not start a second downloader."""
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    scan = jm.Job(title="Library scan", status=jm.JobStatus.AWAITING_REVIEW)
    scan.candidates = [{"cid": "c1", "payload": {"album_id": "scan-X"}}]
    jm.registry.add(scan)
    try:
        r = client.post("/download", data={"album_id": "scan-X"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert scan.id in r.headers["location"]
    finally:
        _remove_job(scan)


# ── POST /jobs/{id}/approve for nonexistent job redirects ──────────

def test_approve_nonexistent_job_redirects_to_queue(client):
    r = client.post("/jobs/nonexistent-id/approve", data={},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/queue"


# ── queue badge count ───────────────────────────────────────────────

def test_queue_badge_shows_pending_count(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp.job_mgr, "registry", jm.JobRegistry())
    j1 = _inject_album_job("badge-test-1")
    j2 = _inject_album_job("badge-test-2")
    try:
        r = client.get("/")
        assert r.status_code == 200
        assert 'class="badge badge-primary badge-sm">2</span>' in r.text
    finally:
        _remove_job(j1)
        _remove_job(j2)


def test_queue_badge_absent_when_no_pending(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp.job_mgr, "registry", jm.JobRegistry())
    r = client.get("/")
    assert r.status_code == 200
    assert "badge-primary" not in r.text


# ── jobs without artist field render safely ─────────────────────────

def test_queue_page_with_artist_none_renders_safe(client):
    job = jm.Job(title="No-artist job", artist="")
    jm.registry.add(job)
    r = client.get("/queue")
    assert r.status_code == 200
    assert "None" not in r.text


def test_dashboard_with_artist_none_renders_safe(client):
    job = jm.Job(title="No-artist job", artist="")
    jm.registry.add(job)
    r = client.get("/")
    assert r.status_code == 200
    assert "None" not in r.text


# ── first-run no-credentials CTA ────────────────────────────────────

def test_dashboard_no_creds_shows_cta(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {})
    r = client.get("/")
    assert r.status_code == 200
    assert "Set up your Qobuz account" in r.text


# ── SSE stream event delivery ───────────────────────────────────────

def test_sse_stream_done_for_finished_job(client):
    job = jm.Job(title="finished")
    job.status = jm.JobStatus.DONE
    jm.registry.add(job)
    with client.stream("GET", f"/api/jobs/{job.id}/stream") as r:
        assert r.status_code == 200
        for chunk in r.iter_text():
            if "event: done" in chunk:
                break
        else:
            pytest.fail("SSE stream never sent 'event: done' for a finished job")


def test_sse_terminal_job_sends_done_without_blocking():
    """A job already terminal at subscribe-time must yield done in the first
    pass, not wait 500ms for the queue timeout."""
    import time as _time

    from qobuz_fetch.web.app import job_stream
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
        # Pre-fix this took 500ms+ on the empty-queue timeout.
        assert elapsed < 0.4, f"terminal-job SSE blocked {elapsed:.2f}s"
    finally:
        _remove_job(job)


def test_sse_stream_done_for_awaiting_review_job(client):
    job = jm.Job(title="awaiting")
    job.status = jm.JobStatus.AWAITING_REVIEW
    jm.registry.add(job)
    with client.stream("GET", f"/api/jobs/{job.id}/stream") as r:
        assert r.status_code == 200
        for chunk in r.iter_text():
            if "event: done" in chunk:
                break
        else:
            pytest.fail("SSE stream never sent 'event: done' for awaiting-review job")


def test_sse_stream_done_on_stream_end_sentinel(client):
    """A live STREAM_END line pushed after the stream starts must yield event: done."""
    import threading

    job = jm.Job(title="running-then-end")
    job.status = jm.JobStatus.RUNNING
    jm.registry.add(job)

    def _push_end():
        # Give the server a beat to subscribe before pushing the sentinel.
        time.sleep(0.2)
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


def test_test_auth_prefers_form_token_over_disk(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    seen = {}
    def _get_token_disk():
        seen["disk"] = True
        return "DISK_TOK"
    monkeypatch.setattr(webapp, "_get_token", _get_token_disk)
    def _probe(endpoint, params, token):
        seen["probed_with"] = token
        return {}
    monkeypatch.setattr("qobuz_fetch.api.client.qobuz_get", _probe)
    r = client.post(
        "/api/test-auth",
        data={"auth_token": "TYPED_TOK"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert seen.get("probed_with") == "TYPED_TOK"
    assert "disk" not in seen


def test_test_auth_falls_back_to_disk_when_form_blank(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    seen = {}
    monkeypatch.setattr(webapp, "_get_token", lambda: "DISK_TOK")
    def _probe(endpoint, params, token):
        seen["probed_with"] = token
        return {}
    monkeypatch.setattr("qobuz_fetch.api.client.qobuz_get", _probe)
    r = client.post(
        "/api/test-auth",
        data={"auth_token": "   "},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert seen.get("probed_with") == "DISK_TOK"


@pytest.mark.parametrize("path", ["/artist", "/library", "/upgrade", "/repair", "/search"])
def test_scan_pages_show_cta_when_no_creds(client, monkeypatch, path):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {})
    r = client.get(path)
    assert r.status_code == 200
    assert "set them in Settings" in r.text
    assert '<input type="text" name="artist"' not in r.text  # form is hidden
    # The library/upgrade/repair Start buttons must be hidden too.
    assert "Start library scan" not in r.text
    assert "Start upgrade scan" not in r.text
    assert "Start repair scan" not in r.text


@pytest.mark.parametrize("route,data", [
    ("/library", {}),
    ("/artist", {"artist": "Stars of the Lid"}),
    ("/upgrade", {}),
    ("/repair", {}),
])
def test_scan_routes_redirect_to_settings_when_no_creds(client, monkeypatch, route, data):
    import qobuz_fetch.web.app as webapp
    def _no_creds():
        raise SystemExit(1)
    monkeypatch.setattr(webapp, "_get_token", _no_creds)
    r = client.post(route, data=data, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"


def test_scan_library_propagates_authlost_so_job_fails(monkeypatch, tmp_path):
    from qobuz_fetch.api.auth import AuthLost
    from qobuz_fetch.web import flows

    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])

    def _authlost(*a, **k):
        raise AuthLost("token expired")
    monkeypatch.setattr(flows, "resolve_artist", _authlost)

    job = jm.Job(title="lib scan")
    with pytest.raises(AuthLost):
        flows.scan_library(job, "tok")


@pytest.mark.parametrize("status", [jm.JobStatus.RUNNING, jm.JobStatus.SCANNING])
def test_queue_running_and_scanning_jobs_show_cancel_button(client, status):
    job = _inject_job(status)
    try:
        r = client.get("/queue")
        assert r.status_code == 200
        assert f"/jobs/{job.id}/cancel" in r.text
    finally:
        _remove_job(job)


def test_dashboard_does_not_double_surface_awaiting_review(client, monkeypatch):
    """An awaiting_review scan must appear only in the Review card, not also
    in the "N jobs queued" alert."""
    import qobuz_fetch.web.app as webapp
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


def test_dashboard_queue_alert_uses_plural_aware_wording(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp.job_mgr, "registry", jm.JobRegistry())
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})

    j = jm.Job(title="X", status=jm.JobStatus.PENDING)
    jm.registry.add(j)
    try:
        r = client.get("/")
        assert "1 job queued" in r.text
    finally:
        _remove_job(j)

    j1 = jm.Job(title="A", status=jm.JobStatus.PENDING)
    j2 = jm.Job(title="B", status=jm.JobStatus.PENDING)
    jm.registry.add(j1)
    jm.registry.add(j2)
    try:
        r = client.get("/")
        assert "2 jobs queued" in r.text
    finally:
        _remove_job(j1)
        _remove_job(j2)


def test_empty_search_renders_instructional_hint(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    r = client.post("/search", data={"q": "   "})
    assert r.status_code == 200
    assert "Type an artist, album, or Qobuz URL" in r.text


def test_empty_artist_scan_shows_error_banner(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    r = client.post("/artist", data={"artist": "   "}, follow_redirects=True)
    assert r.status_code == 200
    assert "Artist name is required" in r.text


def test_sse_stream_emits_heartbeat_when_idle(client, monkeypatch):
    """When the subscriber queue is idle, a `: ping` keepalive must reach
    the client so reverse proxies don't drop the connection."""
    import threading

    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_SSE_HEARTBEAT_TICKS", 1)

    job = jm.Job(title="idle")
    job.status = jm.JobStatus.RUNNING
    jm.registry.add(job)

    def _end_later():
        time.sleep(0.7)  # Give the generator a couple of empty ticks.
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
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"user_id": "42", "auth_token": "SECRET-TOKEN-XYZ"})
    r = client.get("/settings")
    assert r.status_code == 200
    assert "SECRET-TOKEN-XYZ" not in r.text
    assert "Token is set" in r.text


def test_run_task_authlost_yields_friendly_error():
    from qobuz_fetch.api.auth import AuthLost
    job = jm.Job(title="x")
    def _raise(j):
        raise AuthLost("401")
    jm._run_task(job, _raise)
    assert job.status == jm.JobStatus.FAILED
    assert "Token is expired or invalid" in job.error
    assert "401" in job.log_lines[-1]


def test_run_task_disk_full_yields_friendly_error():
    import errno
    job = jm.Job(title="x")
    def _ensp(j):
        raise OSError(errno.ENOSPC, "No space left on device")
    jm._run_task(job, _ensp)
    assert job.status == jm.JobStatus.FAILED
    assert "Out of disk space" in job.error


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


def test_job_retry_resubmits_failed_download(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(
        "qobuz_fetch.api.search.get_album",
        lambda aid, tok: {"title": "Album", "artist": {"name": "Artist"}, "id": aid},
    )
    captured = {}
    original_submit = jm.submit
    def fake_submit(job, fn):
        captured["job"] = job
    monkeypatch.setattr(jm, "submit", fake_submit)

    failed = jm.Job(title="Album", album_id="abc", status=jm.JobStatus.FAILED)
    failed.error = "old failure"
    jm.registry.add(failed)
    try:
        r = client.post(f"/jobs/{failed.id}/retry", follow_redirects=False)
        assert r.status_code == 303
        new_job = captured.get("job")
        assert new_job is not None
        assert new_job.album_id == "abc"
        assert new_job.id != failed.id
        assert new_job.id in r.headers["location"]
    finally:
        _remove_job(failed)
        if captured.get("job"):
            _remove_job(captured["job"])
        jm.submit = original_submit


def test_job_retry_rejects_non_failed_job(client):
    pending = jm.Job(title="X", album_id="abc", status=jm.JobStatus.PENDING)
    jm.registry.add(pending)
    try:
        r = client.post(f"/jobs/{pending.id}/retry", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/queue"
    finally:
        _remove_job(pending)


def test_scan_repairs_propagates_authlost(monkeypatch, tmp_path):
    from qobuz_fetch.api.auth import AuthLost
    from qobuz_fetch.web import flows

    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)

    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_dir])
    monkeypatch.setattr(flows, "list_artist_album_dirs", lambda d: [album_dir])

    def _authlost(*a, **k):
        raise AuthLost("token expired")
    monkeypatch.setattr(
        "qobuz_fetch.repair_log.scan_dir_for_isrc_repairs", _authlost
    )

    job = jm.Job(title="repair scan")
    with pytest.raises(AuthLost):
        flows.scan_repairs(job, "tok")


def test_healthz_returns_ok(client):
    """A liveness probe must not pull the full dashboard render."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_head_on_root_is_200(client):
    """HEAD / from curl -I / uptime monitors must succeed."""
    r = client.request("HEAD", "/")
    assert r.status_code == 200


def test_server_header_is_stripped(client):
    """Don't leak the ASGI framework name in the Server header."""
    r = client.get("/")
    assert "server" not in {k.lower() for k in r.headers}


def test_search_query_too_long_is_rejected_or_truncated(client):
    """A megabyte search string must not reach the Qobuz API verbatim."""
    r = client.post("/search", data={"q": "a" * 10_000},
                    headers={"HX-Request": "true"})
    assert r.status_code in (200, 422)


def test_search_get_query_too_long_is_rejected(client):
    """GET /search?q=... also caps the query so a 1 MB URL can't ride
    through to Qobuz."""
    r = client.get("/search", params={"q": "a" * 10_000})
    assert r.status_code == 422


def test_dashboard_token_invalid_shows_banner(client, monkeypatch):
    """The stale-token banner fires only when _TOKEN_VALID is explicitly False."""
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", False)
    r = client.get("/")
    assert r.status_code == 200
    assert "saved token isn't authenticating" in r.text


def test_dashboard_token_unverified_hides_banner(client, monkeypatch):
    """A None probe result (network blip, not yet probed) must not nag."""
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", None)
    r = client.get("/")
    assert r.status_code == 200
    assert "saved token isn't authenticating" not in r.text


def test_lyric_retry_alert_pluralizes_correctly(client, monkeypatch):
    """Subject and verb must agree at counts 1 and >1; the alert hides at 0."""
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})

    def _render_dashboard_with_count(count):
        monkeypatch.setattr(
            "qobuz_fetch.integrations.lyrics.load_lyric_retry",
            lambda: list(range(count)),
        )

        class _FakeFile:
            def exists(self):
                return count > 0

        monkeypatch.setattr(
            "qobuz_fetch.config.LYRIC_RETRY_FILE", _FakeFile()
        )
        return client.get("/").text

    body_0 = _render_dashboard_with_count(0)
    assert "lyric retry" not in body_0

    body_1 = _render_dashboard_with_count(1)
    assert "1 track needs a lyric retry" in body_1
    assert "1 tracks" not in body_1
    assert "track need " not in body_1

    body_2 = _render_dashboard_with_count(2)
    assert "2 tracks need a lyric retry" in body_2
    assert "2 track " not in body_2
    assert "tracks needs " not in body_2

    body_5 = _render_dashboard_with_count(5)
    assert "5 tracks need a lyric retry" in body_5
    assert "5 track " not in body_5
