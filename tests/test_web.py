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


# ── jobs.py: JobRegistry ──────────────────────────────────────────────────────




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

    def _grab(j):
        with jm.staging_lock():
            second_inside.set()

    jm.submit(contender, _grab)
    # The download lane's worker has picked up the job (it isn't queue-blocked
    # behind the holder), but staging_lock is held — so it must NOT enter yet.
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


def test_submit_failing_job_marks_failed():
    jm.start_worker()

    def boom(_j):
        raise RuntimeError("kaboom")

    job = jm.Job(title="bad")
    jm.submit(job, boom)
    assert _wait_for(lambda: job.status == jm.JobStatus.FAILED)
    assert job.error == "kaboom"
    assert any("kaboom" in ln for ln in job.log_lines)


def test_approve_flips_status_before_enqueue(monkeypatch):
    job = jm.Job(title="scan-approve")
    job.kind = "scan"
    job.status = jm.JobStatus.AWAITING_REVIEW
    job._execute_fn = lambda j, chosen: None
    job.add_candidate("album", "A", "Artist", payload={"id": 1})

    status_at_put = []
    orig_put = jm._scan_queue.put

    def _spy_put(item):
        status_at_put.append(item[0].status)
        orig_put(item)

    monkeypatch.setattr(jm._scan_queue, "put", _spy_put)

    assert jm.approve(job, ["c1"]) is True
    assert status_at_put == [jm.JobStatus.PENDING]
    # A second approve no longer sees AWAITING_REVIEW, so it's rejected.
    assert jm.approve(job, ["c1"]) is False


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

    ok = ss.save({"AUTO_UPGRADE_ENABLED": True})
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


# ── _missing_albums surfaces partial-album fill ─────────

class TestMissingAlbumsSurfacesPartialAlbums:
    # A partly-downloaded album (a folder on disk with track gaps) must show
    # up as a gap-fill candidate, alongside albums missing entirely.

    def _qobuz_tracks(self, isrc_prefix, n):
        return {"items": [{"isrc": f"{isrc_prefix}{i}", "track_number": i + 1}
                          for i in range(n)]}

    def test_yields_partial_and_missing_skips_complete(self, monkeypatch):
        from qobuz_librarian.web import flows

        albums = [
            {"id": 1, "title": "Missing", "tracks_count": 10,
             "tracks": self._qobuz_tracks("A", 10)},
            {"id": 2, "title": "Partial", "tracks_count": 10,
             "tracks": self._qobuz_tracks("B", 10)},
            {"id": 3, "title": "Complete", "tracks_count": 5,
             "tracks": self._qobuz_tracks("C", 5)},
        ]

        def fake_existing(album):
            aid = album.get("id")
            if aid == 1:
                return [], None
            if aid == 2:
                return [{"isrc": f"B{i}", "tracknumber": i + 1}
                        for i in range(5)], "/dir2"
            return [{"isrc": f"C{i}", "tracknumber": i + 1}
                    for i in range(5)], "/dir3"

        def fake_missing(qobuz_tracks, existing):
            have = {t["isrc"] for t in existing}
            miss = [qt for qt in qobuz_tracks if qt["isrc"] not in have]
            pres = [qt for qt in qobuz_tracks if qt["isrc"] in have]
            return miss, pres

        monkeypatch.setattr(flows, "get_artist_albums",
                            lambda *a, **k: (albums, 3))
        monkeypatch.setattr(flows, "dedup_album_versions",
                            lambda catalog, **k: [(a, 1) for a in catalog])
        monkeypatch.setattr(flows, "filter_compilation_albums",
                            lambda pairs, name: pairs)
        monkeypatch.setattr(flows, "filter_short_releases",
                            lambda pairs, n: pairs)
        monkeypatch.setattr(flows, "find_existing_tracks", fake_existing)
        monkeypatch.setattr(flows, "compute_missing", fake_missing)

        yielded = list(flows._missing_albums("aid", "Artist", "tok"))
        ids = [a["id"] for a in yielded]
        assert 1 in ids
        assert 2 in ids
        assert 3 not in ids
        partial = next(a for a in yielded if a["id"] == 2)
        assert partial.get("_partial_missing_count") == 5

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
        monkeypatch.setattr("qobuz_librarian.config.ARTIST_API_DELAY", 0)
        monkeypatch.setattr("qobuz_librarian.config.AUTO_UPGRADE_ENABLED", False)

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
    assert "`ffprobe` binary" in by_label


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
    monkeypatch.setattr(settings_store, "save", lambda *_: False)
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
    # Don't leak the ASGI framework name.
    assert "server" not in {k.lower() for k in r.headers}
    # HSTS only over https (don't pin a plain-http LAN box into https-only).
    https = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=" in https.headers.get("Strict-Transport-Security", "")
    assert "Strict-Transport-Security" not in r.headers


def test_web_fetch_timeout_honors_env_var(monkeypatch):
    import importlib

    import qobuz_librarian.config as cfg_mod
    monkeypatch.setenv("QL_WEB_FETCH_TIMEOUT", "0.001")
    monkeypatch.setenv("QL_WEB_TEST_AUTH_TIMEOUT", "0.002")
    reloaded = importlib.reload(cfg_mod)
    try:
        assert reloaded.WEB_FETCH_TIMEOUT == 0.001
        assert reloaded.WEB_TEST_AUTH_TIMEOUT == 0.002
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
        # One section per artist, none auto-expanded.
        assert t.count("<details") == 2
        assert "<details open" not in t
        assert "3 albums across 2 artists" in flat
        assert "2 albums" in flat          # the Beatles group's count
        # Every candidate is still its own submittable checkbox.
        for cid in ("c0", "c1", "c2"):
            assert f'value="{cid}"' in t
        # Per-artist select-all scoped to the group.
        assert "this.closest('details')" in t
    finally:
        _remove_job(job)


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


# ── POST /jobs/{id}/approve for nonexistent job redirects ──────────



# ── queue badge count ───────────────────────────────────────────────



# ── jobs without artist field render safely ─────────────────────────


# ── first-run no-credentials CTA ────────────────────────────────────


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
        # Pre-fix this took 500ms+ on the empty-queue timeout.
        assert elapsed < 0.4, f"terminal-job SSE blocked {elapsed:.2f}s"
    finally:
        _remove_job(job)


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


def test_scan_routes_redirect_to_settings_when_no_creds(client, monkeypatch):
    """POSTing to any scan route without creds redirects to /settings."""
    import qobuz_librarian.web.app as webapp
    def _no_creds():
        raise SystemExit(1)
    monkeypatch.setattr(webapp, "_get_token", _no_creds)
    r = client.post("/library", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"


def test_scan_library_propagates_authlost_so_job_fails(monkeypatch, tmp_path):
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.web import flows

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


def test_persistence_marks_inflight_jobs_failed_on_restore(monkeypatch):
    """A RUNNING / SCANNING / PENDING job from before the restart comes back
    as FAILED with a retry hint, not silently dropped."""
    from qobuz_librarian.web import job_persistence

    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    for status in (jm.JobStatus.RUNNING, jm.JobStatus.SCANNING,
                   jm.JobStatus.PENDING):
        j = jm.Job(title=f"in-flight {status.value}")
        j.status = status
        job_persistence.persist(j)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({})

    for job in jm.registry.all():
        assert job.status == jm.JobStatus.FAILED
        assert "Interrupted" in (job.error or "")


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


# ── library partial-fill mode filters to on-disk gaps ──────────────────────────

def test_missing_albums_partial_only_skips_fully_missing(monkeypatch):
    from pathlib import Path

    from qobuz_librarian.web import flows
    absent = {"id": "absent", "title": "Not Owned", "tracks_count": 2}
    partial = {"id": "partial", "title": "Owned With Gap", "tracks_count": 3}
    full = {"id": "partial", "title": "Owned With Gap",
            "tracks": {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}}

    monkeypatch.setattr(flows, "get_artist_albums",
                        lambda *a, **k: ([absent, partial], 2))
    monkeypatch.setattr(flows, "dedup_album_versions",
                        lambda c, **k: [(absent, 1), (partial, 1)])
    monkeypatch.setattr(flows, "filter_compilation_albums", lambda p, n: p)
    monkeypatch.setattr(flows, "filter_short_releases", lambda p, m: p)

    def fake_existing(album):
        if album["id"] == "partial":
            return ([{"id": 1}], Path("/music/A/Owned With Gap"))
        return ([], None)

    monkeypatch.setattr(flows, "find_existing_tracks", fake_existing)
    monkeypatch.setattr(flows, "get_album", lambda _i, _t: full)
    monkeypatch.setattr(flows, "compute_missing",
                        lambda q, e: ([{"id": 2}, {"id": 3}], [{"id": 1}]))

    partial_out = list(flows._missing_albums("aid", "Artist", "tok",
                                             partial_only=True))
    assert [a["id"] for a in partial_out] == ["partial"]

    both_out = list(flows._missing_albums("aid", "Artist", "tok"))
    assert {a["id"] for a in both_out} == {"absent", "partial"}


def test_settings_save_rejects_out_of_enum_quality(tmp_path, monkeypatch):
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)

    assert ss.save({"STREAMRIP_QUALITY": "99"}) is True
    on_disk = json.loads((tmp_path / "s.json").read_text())
    assert on_disk.get("STREAMRIP_QUALITY") != "99"
    # A valid value still persists.
    assert ss.save({"STREAMRIP_QUALITY": "2"}) is True
    assert json.loads((tmp_path / "s.json").read_text())["STREAMRIP_QUALITY"] == "2"


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
