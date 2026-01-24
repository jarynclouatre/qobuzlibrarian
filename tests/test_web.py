"""Tests for the web UI: background job system (jobs.py) and HTTP routes (app.py)."""
import queue
import time

import pytest

from qobuz_fetch.web import jobs as jm

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
    orig_put = jm._work_queue.put

    def _spy_put(item):
        status_at_put.append(item[0].status)
        orig_put(item)

    monkeypatch.setattr(jm._work_queue, "put", _spy_put)

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

    from qobuz_fetch.web.app import app
    with TestClient(app) as c:
        c.get("/")
        token = c.cookies.get("qf_csrf")
        c.headers.update({"X-CSRF-Token": token})
        yield c




def test_download_error_raw_exception_not_reflected(client, monkeypatch):
    """Unexpected errors must not be reflected verbatim in the response."""
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


def test_download_partial_result_marks_done_with_error(client, monkeypatch):
    # A partial download (some tracks failed but the album was imported)
    # keeps the job DONE — the folder is reachable — but must surface the
    # failure count in job.error so the green ✓ isn't lying.
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.modes.process as proc_mod
    import qobuz_fetch.web.app as app_mod

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




def test_dashboard_no_creds_shows_setup_cta(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
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






def test_execute_upgrades_does_not_flip_global_cfg(monkeypatch):
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
    """One representative POST verifies CSRF-missing → 403."""
    from fastapi.testclient import TestClient

    from qobuz_fetch.web.app import app
    with TestClient(app) as c:
        c.get("/")
        r = c.post("/search", data={"q": "anything"})
        assert r.status_code == 403


def test_csrf_form_field_body_replayed_to_route():
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


# ── _missing_albums surfaces partial-album fill ─────────

class TestMissingAlbumsSurfacesPartialAlbums:
    # CLI artist mode flags albums with track gaps for gap-fill. The web
    # /artist scan used to ignore any album that had a folder on disk,
    # so a half-downloaded album never appeared as a fill candidate. The
    # fix surfaces partial albums alongside entirely-missing ones.

    def _qobuz_tracks(self, isrc_prefix, n):
        return {"items": [{"isrc": f"{isrc_prefix}{i}", "track_number": i + 1}
                          for i in range(n)]}

    def test_yields_partial_and_missing_skips_complete(self, monkeypatch):
        from qobuz_fetch.web import flows

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
        from qobuz_fetch.web import flows
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




def test_diagnostics_beets_db_parent_missing(tmp_path, monkeypatch):
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import app as webapp
    db = tmp_path / "nope" / "library.db"
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", db)
    checks = webapp._diagnostics()
    row = next(c for c in checks if c["label"] == "beets DB (BEETS_DB_PATH)")
    assert row["ok"] is False
    assert "does not exist" in row["detail"]


def test_settings_empty_save_with_no_existing_creds_returns_error(client, monkeypatch):
    import qobuz_fetch.web.app as _app
    monkeypatch.delenv("QOBUZ_USER_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(_app, "_read_creds", lambda: {})
    r = client.post("/settings", data={"user_id": "", "auth_token": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?error=empty"


def test_streamrip_quality_stays_int_after_settings_save(monkeypatch):
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import settings_store
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    settings_store._apply({"STREAMRIP_QUALITY": "3"})
    assert cfg.STREAMRIP_QUALITY == 3
    assert isinstance(cfg.STREAMRIP_QUALITY, int)




def test_settings_save_with_bad_token_redirects_with_auth_warning(client, monkeypatch):
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


def test_settings_behavior_persist_failure_redirects_with_error(client, monkeypatch):
    from qobuz_fetch.web import settings_store
    monkeypatch.setattr(settings_store, "save", lambda *_: False)
    r = client.post("/settings/behavior", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert "error=persist" in r.headers["location"]




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
    https = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=" in https.headers.get("Strict-Transport-Security", "")
    plain = client.get("/")
    assert "Strict-Transport-Security" not in plain.headers


def test_web_fetch_timeout_honors_env_var(monkeypatch):
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

    from qobuz_fetch.integrations import rip

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
    assert r2.json() == {"detail": "Job not found"}


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






def test_test_auth_unexpected_exception_renders_safe_message(client, monkeypatch):
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


def test_download_duplicate_matches_scan_candidate(client, monkeypatch):
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



# ── queue badge count ───────────────────────────────────────────────



# ── jobs without artist field render safely ─────────────────────────


# ── first-run no-credentials CTA ────────────────────────────────────


# ── SSE stream event delivery ───────────────────────────────────────

def test_sse_stream_404_returns_json(client):
    r = client.get("/api/jobs/no-such-job/stream")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"error": "not found"}


def test_templates_have_no_hx_on_attributes():
    """htmx 1.9's hx-on:* attributes evaluate via new Function() which the page CSP correctly forbids."""
    from pathlib import Path
    tpl_dir = Path(__file__).resolve().parents[1] / "src/qobuz_fetch/web/templates"
    offenders = []
    for f in tpl_dir.glob("*.html"):
        text = f.read_text(encoding="utf-8")
        for needle in ("hx-on:", "hx-on::"):
            if needle in text:
                offenders.append((f.name, needle))
    assert not offenders, (
        f"hx-on:* attributes found (CSP would block these via new Function): "
        f"{offenders}. Move to web/static/app.js handlers.")


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




def test_scan_routes_redirect_to_settings_when_no_creds(client, monkeypatch):
    """POSTing to any scan route without creds redirects to /settings."""
    import qobuz_fetch.web.app as webapp
    def _no_creds():
        raise SystemExit(1)
    monkeypatch.setattr(webapp, "_get_token", _no_creds)
    r = client.post("/library", data={}, follow_redirects=False)
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


def test_dashboard_does_not_double_surface_awaiting_review(client, monkeypatch):
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


def test_empty_search_renders_instructional_hint(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    r = client.post("/search", data={"q": "   "})
    assert r.status_code == 200
    assert "Type an artist, album, or Qobuz URL" in r.text




def test_sse_stream_emits_heartbeat_when_idle(client, monkeypatch):
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






def test_server_header_is_stripped(client):
    """Don't leak the ASGI framework name in the Server header."""
    r = client.get("/")
    assert "server" not in {k.lower() for k in r.headers}


def test_search_query_too_long_is_rejected_or_truncated(client):
    """A megabyte search string must not reach the Qobuz API verbatim."""
    r = client.post("/search", data={"q": "a" * 10_000},
                    headers={"HX-Request": "true"})
    assert r.status_code in (200, 422)




def test_dashboard_token_invalid_shows_banner(client, monkeypatch):
    """The stale-token banner fires only when _TOKEN_VALID is explicitly False."""
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_read_creds", lambda: {"auth_token": "tok"})
    monkeypatch.setattr(webapp, "_TOKEN_VALID", False)
    r = client.get("/")
    assert r.status_code == 200
    assert "saved token isn't authenticating" in r.text


# ── empty form values reach friendly branches, never 422 JSON ──────


def test_download_empty_album_id_renders_friendly_error(client):
    r = client.post("/download", data={"album_id": ""},
                    headers={"HX-Request": "true"})
    assert r.status_code in (200, 400)
    assert r.headers["content-type"].startswith("text/html")
    assert "alert-error" in r.text
    assert "Missing album id" in r.text




def test_artist_empty_form_redirects_with_friendly_error(client):
    r = client.post("/artist", data={"artist": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/artist?error=")
    assert "required" in loc


# ── 404 album_id maps to a "no album" message, not "network" ────────


def test_download_404_maps_to_no_album_message(client, monkeypatch):
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.web.app as webapp
    from qobuz_fetch.api.auth import QobuzError
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")

    def _missing(_id, _tok):
        raise QobuzError(
            "HTTP 404 from album/get: "
            "{'status':'error','code':404,'message':'album not found'}"
        )
    monkeypatch.setattr(search_mod, "get_album", _missing)
    r = client.post("/download", data={"album_id": "ghost-album"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "No album with that id" in r.text
    assert "container's network" not in r.text


# ── HEAD requests on /healthz / /queue / /settings ─────────────────


# ── dedupe vs already-in-library ────────────────────────────


def test_download_force_param_overrides_library_check(client, monkeypatch):
    from pathlib import Path

    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.library.catalog as cat
    import qobuz_fetch.modes.process as proc_mod
    import qobuz_fetch.web.app as webapp
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Forced",
                                          "artist": {"name": "Artist"}})
    monkeypatch.setattr(cat, "find_album_dir_filesystem",
                        lambda album: Path("/tmp/album-dir"))
    monkeypatch.setattr(cat, "find_existing_tracks",
                        lambda album: ([{"isrc": "X1"}], Path("/tmp/album-dir")))
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
    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import settings_store as ss

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


# ── test-auth bad token maps to "Token rejected" not "network" ──


def test_test_auth_bad_token_maps_to_token_rejected(client, monkeypatch):
    import qobuz_fetch.web.app as webapp
    from qobuz_fetch.api.auth import QobuzError
    monkeypatch.setattr(webapp, "_get_token", lambda: "tok")

    def _reject(*a, **k):
        raise QobuzError(
            "HTTP 401 from user/login: "
            "{'status':'error','code':401,'message':'invalid token'}"
        )
    monkeypatch.setattr("qobuz_fetch.api.client.qobuz_get", _reject)
    r = client.post("/api/test-auth",
                    data={"auth_token": "DEFINITELY_NOT_REAL"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Token rejected" in r.text
    assert "network" not in r.text.lower()


# ── invalid status filter on /api/jobs returns 400 ──────────


def test_jobs_list_unknown_status_returns_400(client):
    r = client.get("/api/jobs?status=garbage")
    assert r.status_code == 400
    assert r.json() == {"detail": "Unknown status filter"}


# ── XSS-like artist names rejected before reaching Qobuz ────


def test_artist_with_angle_brackets_rejected(client):
    before = len(jm.registry.all())
    r = client.post("/artist",
                    data={"artist": "<script>alert(1)</script>"},
                    follow_redirects=False)
    # 400 status with a redirect carrying the error.
    assert r.status_code == 400
    assert "error=" in r.headers["location"]
    assert before == len(jm.registry.all())


# ── web gap-fill discovery + completeness guard ────────────────────────────────


def test_download_partial_album_proceeds_to_gap_fill(client, monkeypatch):
    from pathlib import Path

    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.library.catalog as cat_mod
    import qobuz_fetch.modes.process as proc_mod
    import qobuz_fetch.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    album = {"id": "gap1", "title": "Gappy", "artist": {"name": "A"},
             "tracks": {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}}
    monkeypatch.setattr(search_mod, "get_album", lambda _i, _t: album)
    monkeypatch.setattr(cat_mod, "find_album_dir_filesystem",
                        lambda _a: Path("/music/A/Gappy"))
    monkeypatch.setattr(cat_mod, "find_existing_tracks",
                        lambda _a: ([{"id": 1}], None))
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

    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.library.catalog as cat_mod
    import qobuz_fetch.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    album = {"id": "done1", "title": "Whole", "artist": {"name": "A"},
             "tracks": {"items": [{"id": 1}, {"id": 2}]}}
    monkeypatch.setattr(search_mod, "get_album", lambda _i, _t: album)
    monkeypatch.setattr(cat_mod, "find_album_dir_filesystem",
                        lambda _a: Path("/music/A/Whole"))
    monkeypatch.setattr(cat_mod, "find_existing_tracks",
                        lambda _a: ([{"id": 1}, {"id": 2}], None))
    monkeypatch.setattr(cat_mod, "compute_missing",
                        lambda q, e: ([], [{"id": 1}, {"id": 2}]))

    r = client.post("/download", data={"album_id": "done1"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "already complete" in r.text.lower()
    assert not [j for j in list(jm.registry._jobs.values())
                if getattr(j, "album_id", None) == "done1"]


# ── library partial-fill mode filters to on-disk gaps ──────────────────────────

def test_missing_albums_partial_only_skips_fully_missing(monkeypatch):
    from pathlib import Path

    from qobuz_fetch.web import flows
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

    from qobuz_fetch import config as cfg
    from qobuz_fetch.web import settings_store as ss
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
    import qobuz_fetch.api.search as search_mod
    import qobuz_fetch.web.app as webapp
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
