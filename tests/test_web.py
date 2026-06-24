"""Tests for the web UI: background job system (jobs.py) and HTTP routes (app.py).

Trimmed to a maintainable representative set: data-safety paths (restore,
hide/restore round-trip, migration move-vs-copy, persist-without-tearing),
auth/session/CSRF, the run-lock destructive-route guard, settings save/load,
one search + one approve endpoint, and a few genuinely tricky bits of logic.
"""
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


# ── jobs.py: worker loop ──────────────────────────────────────────────────────

def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


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


def test_search_uses_a_generous_result_limit(client, monkeypatch):
    # The front-page search was capped at 8, so a major artist surfaced almost
    # nothing (the owner's first complaint). The handler must pass the configured
    # limit through to Qobuz, and that default must be generous.
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    seen = {}

    def fake(q, t, limit=None):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(search_mod, "search_albums", fake)
    r = client.post("/search", data={"q": "Paul McCartney"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert seen.get("limit") == cfg.SEARCH_LIMIT
    assert cfg.SEARCH_LIMIT >= 20


def test_new_release_check_refused_without_baseline(client, monkeypatch):
    # "Check for new releases" is a library-walk-and-compare — useless until a full
    # library scan has built the baseline. Without one it must NOT start a crawl
    # (the old bug: it ran an empty crawl AND flipped baseline_complete=True, which
    # then stopped an interrupted library scan from resuming). It refuses instead.
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.library import new_releases
    from qobuz_librarian.web import flows
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(flows, "scan_new_releases", lambda *a, **k: None)
    assert new_releases.is_baseline_complete() is False      # fresh state, no baseline
    r = client.post("/library", data={"mode": "new_releases"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/library")
    assert app_mod._existing_new_release_check() is None     # no crawl was started


def test_library_scan_state_explains_empty_music_root(tmp_path, monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod.cfg, "MUSIC_ROOT", tmp_path)
    state = app_mod._library_scan_state()

    assert state["ready"] is False
    assert "MUSIC_ROOT" in state["message"]
    assert "artist" in state["message"].lower()
    assert "QL_MUSIC_DIR" not in state["message"]


def test_qobuz_ready_false_when_saved_token_is_rejected(monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"auth_token": "bad-token", "user_id": "user"})
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", False)

    assert app_mod._qobuz_ready() is False


def test_qobuz_ready_allows_unproven_saved_token(monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"auth_token": "maybe-token", "user_id": "user"})
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", None)

    assert app_mod._qobuz_ready() is True


def test_recent_empty_hint_matches_qobuz_account_state(monkeypatch):
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"auth_token": "bad-token", "user_id": "user"})
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", False)
    assert app_mod._recent_empty_hint() == "Reconnect Qobuz before searching."

    monkeypatch.setattr(app_mod, "_read_creds", lambda: {})
    monkeypatch.setattr(app_mod, "_TOKEN_VALID", None)
    assert app_mod._recent_empty_hint() == "Set up Qobuz before searching."

    monkeypatch.setattr(app_mod, "_read_creds",
                        lambda: {"auth_token": "maybe-token", "user_id": "user"})
    assert app_mod._recent_empty_hint() == "Search above to find an album or artist."


def test_empty_library_scan_message_uses_music_root(monkeypatch, caplog):
    from qobuz_librarian.web import flows

    monkeypatch.setattr(flows, "list_library_artists", lambda: [])
    caplog.set_level("INFO", logger="qobuz_librarian")
    job = jm.Job(title="scan")

    flows.scan_library(job, "tok")

    assert "MUSIC_ROOT" in job.summary
    assert "QL_MUSIC_DIR" not in job.summary
    out = caplog.text
    assert "MUSIC_ROOT" in out
    assert "QL_MUSIC_DIR" not in out


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


# ── CSRF middleware ───────────────────────────────────────────────────────────

def test_csrf_post_without_token_is_rejected():
    """One representative POST verifies CSRF-missing → 403."""
    from fastapi.testclient import TestClient

    from qobuz_librarian.web.app import app
    with TestClient(app) as c:
        c.get("/")
        r = c.post("/search", data={"q": "anything"})
        assert r.status_code == 403


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
            ("/downsample", {}),
            ("/repair", {}),
            ("/lyrics", {}),
            ("/lyric-retry", {}),
            ("/jobs/whatever/approve", {}),
        ]:
            r = c.post(path, data=data, follow_redirects=False)
            assert r.status_code == 503, f"{path} should 503 when lock busy"
            # The full-page response should render the base shell, not a bare
            # <pre>, so a non-htmx caller still has navigation back.
            assert "navbar" in r.text, f"{path} should render base.html shell"
            assert 'class="btn btn-primary w-full sm:w-auto">Try again</button>' in r.text
            assert 'class="btn btn-ghost w-full sm:w-auto">Back to dashboard</a>' in r.text


def test_error_page_action_is_mobile_friendly(client):
    r = client.get("/not-a-real-page")

    assert r.status_code == 404
    assert "Page not found" in r.text
    assert 'href="/" class="btn btn-primary w-full sm:w-auto"' in r.text


def test_tool_pages_link_to_focused_artist_scans(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    for path in ("/upgrade", "/downsample", "/repair", "/lyrics"):
        r = client.get(path)
        assert r.status_code == 200
        assert "Just one artist?" in r.text
        assert 'href="/artist"' in r.text


def test_scan_action_buttons_are_mobile_friendly(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})
    monkeypatch.setattr(
        "qobuz_librarian.integrations.downsample_engine.HAVE_DOWNSAMPLE",
        True,
    )
    monkeypatch.setattr(
        "qobuz_librarian.integrations.lyric_fetch.AVAILABLE",
        True,
    )
    for path in ("/library", "/upgrade", "/downsample", "/repair", "/lyrics"):
        r = client.get(path)
        assert r.status_code == 200
        assert 'class="btn btn-primary w-full sm:w-auto"' in r.text


def test_dashboard_search_button_label_is_visible_on_mobile(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/")

    assert r.status_code == 200
    assert 'class="flex flex-col gap-2 sm:flex-row"' in r.text
    assert 'class="input input-bordered input-lg min-w-0 flex-1"' in r.text
    assert 'class="btn btn-primary btn-lg w-full gap-2 px-4 sm:w-auto sm:px-6"' in r.text
    assert '<span>Search</span>' in r.text
    assert '<span class="hidden sm:inline">Search</span>' not in r.text


def test_dashboard_setup_scan_action_stacks_on_mobile(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/")

    assert r.status_code == 200
    assert 'class="w-full shrink-0 sm:ml-auto sm:w-auto"' in r.text
    assert 'class="btn btn-sm btn-primary w-full sm:w-auto"' in r.text


def test_search_page_form_stacks_cleanly_on_mobile(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/search")

    assert r.status_code == 200
    assert "Artist, album, or URL" in r.text
    assert "Artist, album, or Qobuz URL" not in r.text
    assert "Type an artist, album, or URL above to search." in r.text
    assert "autofocus" not in r.text
    assert 'class="flex flex-col gap-2 sm:flex-row"' in r.text
    assert 'class="input input-bordered input-lg w-full min-w-0 flex-1"' in r.text
    assert 'class="btn btn-primary btn-lg w-full gap-2 sm:w-auto"' in r.text


def test_artist_page_form_stacks_cleanly_on_mobile(client, monkeypatch):
    from qobuz_librarian.web import app as webapp

    monkeypatch.setattr(webapp, "_read_creds",
                        lambda: {"auth_token": "dummy", "user_id": "dummy"})

    r = client.get("/artist")

    assert r.status_code == 200
    assert "autofocus" not in r.text
    assert 'class="flex flex-col gap-2 sm:flex-row"' in r.text
    assert 'class="input input-bordered input-lg w-full min-w-0 flex-1"' in r.text
    assert 'class="btn btn-primary btn-lg w-full sm:w-auto"' in r.text
    assert 'class="grid grid-cols-2 gap-2 sm:grid-cols-4"' in r.text
    assert 'class="btn btn-sm btn-outline w-full sm:w-auto"' in r.text


def test_queue_empty_state_has_clear_actions(client):
    r = client.get("/queue")

    assert r.status_code == 200
    assert "Queue is empty." in r.text
    assert "New downloads, scans, and reviews appear here" in r.text
    assert 'href="/search" class="btn btn-primary w-full sm:w-auto"' in r.text
    assert 'href="/queue/history" class="btn btn-outline w-full sm:w-auto"' in r.text


def test_history_empty_state_has_clear_action(client):
    r = client.get("/queue/history")

    assert r.status_code == 200
    assert "No finished jobs yet." in r.text
    assert "Completed downloads, scans, and reviews show up here." in r.text
    assert 'href="/queue" class="btn btn-outline w-full sm:w-auto"' in r.text


def test_hidden_empty_state_has_mobile_friendly_action(client):
    r = client.get("/library/hidden")

    assert r.status_code == 200
    assert "Nothing hidden." in r.text
    assert 'href="/library" class="btn btn-primary w-full sm:w-auto mt-4"' in r.text


def test_repair_history_empty_state_has_mobile_friendly_action(client):
    r = client.get("/repair/history")

    assert r.status_code == 200
    assert "Nothing repaired yet." in r.text
    assert 'href="/repair" class="btn btn-primary w-full sm:w-auto mt-4"' in r.text


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


def test_dashboard_active_non_download_job_uses_neutral_wording(client, monkeypatch):
    import qobuz_librarian.web.app as app_mod

    job = jm.Job(title="Lyrics sweep", status=jm.JobStatus.RUNNING)
    job.execute_kind = "lyrics"
    job.push_progress("Fetching lyrics", 3, 12, unit="track")
    monkeypatch.setattr(app_mod.job_mgr.registry, "pending_and_running",
                        lambda: [job])

    r = client.get("/")

    assert r.status_code == 200
    assert "Working" in r.text
    assert "Fetching lyrics 3 / 12 tracks" in r.text
    assert 'aria-label="Cancel job"' in r.text
    assert "Cancel download" not in r.text
    assert "Cancel scan" not in r.text


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


def test_review_zero_selection_has_clear_disabled_action(client):
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994"}, selected=False)
    try:
        r = client.get(f"/jobs/{job.id}")
        assert r.status_code == 200
        assert "Select albums to download" in r.text
        assert "Download 1 selected" not in r.text
    finally:
        _remove_job(job)


def test_review_footer_actions_stack_on_mobile(client):
    job = _inject_job(jm.JobStatus.AWAITING_REVIEW)
    job.execute_kind = "library"
    job.review_verb = "Download"
    job.add_candidate(kind="album", title="Dummy", artist="Portishead",
                      payload={"year": "1994"}, selected=True)
    try:
        r = client.get(f"/jobs/{job.id}")

        assert r.status_code == 200
        assert 'id="review-submit" class="btn btn-primary w-full sm:w-auto"' in r.text
        assert 'class="btn btn-ghost w-full sm:w-auto">Cancel</button>' in r.text
        assert 'class="btn btn-ghost btn-sm w-full sm:ml-auto sm:w-auto">Hidden albums</a>' in r.text
    finally:
        _remove_job(job)


def test_library_bulk_hide_marked_artists_keeps_selected_albums(client, monkeypatch, tmp_path):
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
    job.add_candidate(kind="album", title="Mezzanine", artist="Massive Attack",
                      payload={"year": "1998"}, selected=False)
    try:
        r = client.post(f"/jobs/{job.id}/select",
                        data={"cid": c_dummy, "checked": "1"})
        assert r.status_code == 200

        r = client.post(
            f"/jobs/{job.id}/hide-artists",
            content="artist=Portishead&artist=Burial",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 200
        assert r.json()["hidden"] == 2
        assert r.json()["total"] == 2
        assert r.json()["selected"] == 1

        survivors = {c["artist"] + "/" + c["title"]: c["selected"]
                     for c in job.candidates}
        assert survivors == {
            "Portishead/Dummy": True,
            "Massive Attack/Mezzanine": False,
        }
        store = hidden.load()
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Third", store)
        assert hidden.is_hidden(hidden.SCOPE_MISSING, "Burial", "Untrue", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
        assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Massive Attack", "Mezzanine", store)
    finally:
        _remove_job(job)


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


def test_settings_cli_only_consolidate_is_read_only_and_preserved(
        client, tmp_path, monkeypatch):
    import json

    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import settings_store as ss

    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ss, "_any_active_job", lambda: False)
    monkeypatch.setattr(cfg, "CONSOLIDATE", True)

    r = client.get("/settings")

    assert r.status_code == 200
    assert 'id="cb-CONSOLIDATE"' not in r.text
    assert "CLI only" in r.text
    assert 'name="CONSOLIDATE" value="1"' in r.text

    data = {"form_complete": "1", "CONSOLIDATE": "1"}
    data.update({key: "" for key in ss.TEXT_KEYS})
    r = client.post("/settings/behavior", data=data, follow_redirects=False)

    assert r.status_code == 303
    assert json.loads((tmp_path / "s.json").read_text())["CONSOLIDATE"] is True


def test_settings_primary_actions_are_mobile_friendly(client):
    r = client.get("/settings")

    assert r.status_code == 200
    assert 'class="input input-bordered min-w-0 flex-1 font-mono text-base sm:text-sm"' in r.text
    assert 'class="btn btn-primary w-full sm:w-auto">Save &amp; connect</button>' in r.text
    assert 'class="btn btn-outline btn-sm w-full sm:w-auto">Hand off to terminal</button>' in r.text
    assert 'class="btn btn-primary w-full sm:w-auto">Save behaviour</button>' in r.text


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


def test_login_form_password_row_is_mobile_safe(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path) as c:
        r = c.get("/login")

    assert r.status_code == 200
    assert 'class="input input-bordered min-w-0 flex-1"' in r.text


def test_setup_form_password_rows_are_mobile_safe(monkeypatch, tmp_path):
    with _enable_auth(monkeypatch, tmp_path, configure=False) as c:
        r = c.get("/setup")

    assert r.status_code == 200
    assert r.text.count('class="input input-bordered min-w-0 flex-1"') == 2


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


def test_malformed_host_cannot_bypass_auth(monkeypatch, tmp_path):
    # CVE-2026-48710: Starlette rebuilds request.url.path from the client Host
    # header, so a host like "example.com/login?x=" can make the auth middleware
    # read the path as "/login" and wave a protected route through with no
    # session. The gate reads request.scope["path"] (the real routed path),
    # which a forged Host cannot touch — protected routes stay closed.
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


def test_artist_sort_key_files_articles_under_the_real_letter():
    # "The Beatles" must sort under B, not T (owner acceptance criterion);
    # leading the/a/an are ignored, the rest of the name is not.
    from qobuz_librarian.web.app import _artist_sort_key
    names = ["The Beatles", "Bob Dylan", "ABBA", "Adele", "The Who",
             "A Tribe Called Quest", "an Evening"]
    ordered = sorted(names, key=_artist_sort_key)
    assert ordered.index("Adele") < ordered.index("The Beatles") < ordered.index("Bob Dylan")
    assert ordered[-1] == "The Who"            # "who" sorts last
    assert _artist_sort_key("The Beatles") == "beatles"
    assert _artist_sort_key("A Tribe Called Quest") == "tribe called quest"
    assert _artist_sort_key("Adele") == "adele"   # no leading "a " to strip


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


def test_migrate_preview_action_is_mobile_friendly(client, monkeypatch, tmp_path):
    import qobuz_librarian.config as cfg

    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(cfg, "MIGRATE_SRC", str(src))
    monkeypatch.setattr(cfg, "MIGRATE_DEST", str(tmp_path / "dest"))

    r = client.get("/migrate")

    assert r.status_code == 200
    assert 'class="btn btn-primary w-full sm:w-auto">Preview migration</button>' in r.text


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
