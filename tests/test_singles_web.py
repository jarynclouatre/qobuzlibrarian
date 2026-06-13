"""Web path for single-track grabs: the Tracks search mode, the Get-track
download contract (marks the album single), and graduation (completing the album
the normal way clears the mark)."""
import pytest
from test_web import _remove_job, _wait_for, client  # noqa: F401 (fixture)

from qobuz_librarian.library import hidden
from qobuz_librarian.web import jobs as jm


@pytest.fixture
def fresh_singles(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")


def test_track_search_renders_get_track_rows(client, monkeypatch):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod
    monkeypatch.setattr(app_mod, "_read_creds", lambda: {"auth_token": "x"})
    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "search_tracks", lambda *a, **k: [{
        "id": "trk7", "title": "Black Eye", "version": "Radio Edit",
        "track_number": 3, "maximum_bit_depth": 24,
        "album": {"id": "alb1", "title": "Girl With No Face",
                  "artist": {"name": "Allie X"}, "tracks_count": 11,
                  "image": {"small": "https://static.qobuz.com/x.jpg"}},
    }])
    r = client.post("/search", data={"q": "black eye", "kind": "track"})
    assert r.status_code == 200
    assert "Get track" in r.text
    assert "Black Eye" in r.text
    assert "track 3 of 11" in r.text
    # album-mode download button must NOT be the offered action here
    assert 'name="track_id"' in r.text


def test_get_track_downloads_one_and_marks_the_album_single(client, monkeypatch, fresh_singles):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.queue.executor as ex_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "alb1", "title": "Girl With No Face", "year": 2024,
        "artist": {"name": "Allie X"},
        "tracks": {"items": [
            {"id": "trk7", "title": "Black Eye", "track_number": 3},
            {"id": "trk8", "title": "Galina", "track_number": 4},
            {"id": "trk9", "title": "Off With Her Tits", "track_number": 5}]}})
    # own none of it, so the grabbed track leaves the album partial -> a single
    monkeypatch.setattr(cat_mod, "find_existing_tracks", lambda *a, **k: ([], None))

    def fake_exec(queue, *a, **k):
        queue[0]["n_ok"] = 1
        queue[0]["imported"] = True
        queue[0]["n_fail"] = 0
    monkeypatch.setattr(ex_mod, "_execute_download_queue", fake_exec)

    jm.start_worker()
    r = client.post("/download", data={"album_id": "alb1", "track_id": "trk7"},
                    follow_redirects=False)
    assert r.status_code in (200, 303)
    jobs = [j for j in list(jm.registry._jobs.values())
            if getattr(j, "album_id", None) == "alb1"]
    assert len(jobs) == 1
    job = jobs[0]
    try:
        assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        assert job.status == jm.JobStatus.DONE
        assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is True
    finally:
        _remove_job(job)


def test_get_track_already_owned_is_a_noop(client, monkeypatch, fresh_singles):
    # Grabbing a track you already own must NOT re-rip it (which would land a
    # beets ".1.flac" duplicate) and must NOT mark the owned album a single.
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.queue.executor as ex_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "alb1", "title": "Some Time In New York City",
        "artist": {"name": "John Lennon"},
        "tracks": {"items": [{"id": "trk8", "title": "John Sinclair", "track_number": 8}]}})
    monkeypatch.setattr(cat_mod, "find_existing_tracks",
                        lambda *a, **k: ([{"path": "/m/a/08.flac"}], "/m/a"))
    monkeypatch.setattr(cat_mod, "compute_missing", lambda *a, **k: ([], ["have it"]))
    called = {"exec": False}
    monkeypatch.setattr(ex_mod, "_execute_download_queue",
                        lambda *a, **k: called.__setitem__("exec", True))

    jm.start_worker()
    r = client.post("/download", data={"album_id": "alb1", "track_id": "trk8"},
                    follow_redirects=False)
    assert r.status_code in (200, 303)
    job = [j for j in list(jm.registry._jobs.values())
           if getattr(j, "album_id", None) == "alb1"][0]
    try:
        assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        assert job.status == jm.JobStatus.DONE
        assert called["exec"] is False  # never downloaded
        assert hidden.is_single("John Lennon", "Some Time In New York City",
                                hidden.load()) is False
        assert "already have" in (job.summary or "").lower()
    finally:
        _remove_job(job)


def test_get_track_for_a_track_not_on_the_album_is_rejected(client, monkeypatch, fresh_singles):
    # If the track can't be resolved on the album (a stub/empty track list, or a
    # stale id), reject at the route — never queue a job that would read the empty
    # missing-set as "you already own it" and silently mark the album a single.
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.web.app as app_mod

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "alb1", "title": "Girl With No Face",
        "artist": {"name": "Allie X"}, "tracks": {"items": []}})

    jm.start_worker()
    r = client.post("/download", data={"album_id": "alb1", "track_id": "trk7"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert not [j for j in list(jm.registry._jobs.values())
                if getattr(j, "album_id", None) == "alb1"]
    assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is False


def test_undo_removes_the_grabbed_track_and_clears_the_mark(client, monkeypatch, fresh_singles, tmp_path):
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.scanner as scanner_mod

    d = tmp_path / "Allie X" / "Girl With No Face (2024)"
    d.mkdir(parents=True)
    f = d / "03 - Black Eye.flac"
    f.write_bytes(b"flac")
    hidden.mark_single("Allie X", "Girl With No Face", "2024", "alb1")

    job = jm.Job(title="Black Eye", artist="Allie X", album_id="alb1")
    job.status = jm.JobStatus.DONE
    job.single = {"album_id": "alb1", "track_id": "trk7", "dir": str(d),
                  "isrc": "ISRC1", "track_no": 3, "title": "Black Eye",
                  "artist": "Allie X", "album": "Girl With No Face",
                  "marked": True, "new_folder": False}
    jm.registry.add(job)
    monkeypatch.setattr(scanner_mod, "read_album_dir",
                        lambda _d: [{"path": str(f), "isrc": "ISRC1", "track": 3}])
    monkeypatch.setattr(beets_mod, "forget_beets_entries", lambda paths: len(paths))
    try:
        r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert r.status_code in (200, 303)
        assert not f.exists()  # the grabbed track is gone
        assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is False
        assert job.single.get("removed") is True
    finally:
        _remove_job(job)


def test_undo_keeps_multidisc_album_dir_with_audio_in_disc_subdirs(
        client, monkeypatch, fresh_singles, tmp_path):
    # The grab files a track into a beets multi-disc layout ($album/Disc N/...).
    # Undo removes the grabbed track but must NOT rmtree the whole album when
    # other tracks still live in 'Disc N/' subdirs — the old shallow .flac check
    # at the album root saw none and deleted everything.
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.scanner as scanner_mod

    d = tmp_path / "Artist" / "Box Set (2020)"
    disc = d / "Disc 1"
    disc.mkdir(parents=True)
    grabbed = disc / "03 - Grabbed.flac"
    keep = disc / "01 - Keep.flac"
    grabbed.write_bytes(b"flac")
    keep.write_bytes(b"flac")

    job = jm.Job(title="Grabbed", artist="Artist", album_id="alb9")
    job.status = jm.JobStatus.DONE
    job.single = {"album_id": "alb9", "track_id": "trk3", "dir": str(d),
                  "isrc": "ISRCG", "track_no": 3, "title": "Grabbed",
                  "artist": "Artist", "album": "Box Set",
                  "marked": False, "new_folder": True}
    jm.registry.add(job)
    monkeypatch.setattr(scanner_mod, "read_album_dir", lambda _d: [
        {"path": str(grabbed), "isrc": "ISRCG", "track": 3},
        {"path": str(keep), "isrc": "ISRCK", "track": 1}])
    monkeypatch.setattr(beets_mod, "forget_beets_entries", lambda paths: len(paths))
    try:
        r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert r.status_code in (200, 303)
        assert not grabbed.exists()      # grabbed track removed
        assert keep.exists()             # the other disc track survives
        assert d.is_dir()                # album NOT rmtree'd
    finally:
        _remove_job(job)


def test_completing_the_album_normally_graduates_the_single(client, monkeypatch, fresh_singles):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.modes.process as proc_mod
    import qobuz_librarian.web.app as app_mod

    # pre-existing single mark for this album
    hidden.mark_single("Allie X", "Girl With No Face", "2024", "alb1")
    assert hidden.is_single("Allie X", "Girl With No Face", hidden.load())

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "alb1", "title": "Girl With No Face",
        "artist": {"name": "Allie X"}, "tracks": {"items": []}})
    monkeypatch.setattr(proc_mod, "process_album", lambda *a, **k: {
        "result": "ok", "n_ok": 11, "n_fail": 0, "imported": True})

    jm.start_worker()
    r = client.post("/download", data={"album_id": "alb1"}, follow_redirects=False)
    assert r.status_code in (200, 303)
    jobs = [j for j in list(jm.registry._jobs.values())
            if getattr(j, "album_id", None) == "alb1"]
    job = jobs[0]
    try:
        assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        # graduating: the album is now claimed, so the single mark is gone
        assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is False
    finally:
        _remove_job(job)


def test_grab_completing_the_album_clears_a_prior_single_mark(client, monkeypatch, fresh_singles):
    # Grabbing the album's LAST missing track completes it — it's a full album
    # now, so a single mark left by an earlier partial grab must be cleared, or
    # the artist stays wrongly hidden from bulk scans and the new-release check.
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.queue.executor as ex_mod
    import qobuz_librarian.web.app as app_mod

    hidden.mark_single("Allie X", "Girl With No Face", "2024", "alb1")
    assert hidden.is_single("Allie X", "Girl With No Face", hidden.load())

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "alb1", "title": "Girl With No Face", "year": 2024,
        "artist": {"name": "Allie X"},
        "tracks": {"items": [
            {"id": "trk7", "title": "Black Eye", "track_number": 3},
            {"id": "trk8", "title": "Galina", "track_number": 4}]}})
    # Own everything but trk7, so this grab is the album's last missing track.
    monkeypatch.setattr(cat_mod, "find_existing_tracks",
                        lambda *a, **k: ([{"path": "/m/a/04.flac"}], "/m/a"))
    monkeypatch.setattr(cat_mod, "compute_missing", lambda *a, **k: (
        [{"id": "trk7", "title": "Black Eye", "track_number": 3}], ["have trk8"]))

    def fake_exec(queue, *a, **k):
        queue[0]["n_ok"] = 1
        queue[0]["imported"] = True
        queue[0]["n_fail"] = 0
    monkeypatch.setattr(ex_mod, "_execute_download_queue", fake_exec)

    jm.start_worker()
    r = client.post("/download", data={"album_id": "alb1", "track_id": "trk7"},
                    follow_redirects=False)
    assert r.status_code in (200, 303)
    job = [j for j in list(jm.registry._jobs.values())
           if getattr(j, "album_id", None) == "alb1"][0]
    try:
        assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        assert job.status == jm.JobStatus.DONE
        assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is False
    finally:
        _remove_job(job)


def test_undo_matches_by_track_number_when_grab_had_no_isrc(
        client, monkeypatch, fresh_singles, tmp_path):
    # An ISRC-less grab is reversed by the on-disk track NUMBER, which the
    # scanner keys as "tracknumber". It must remove the grabbed track and leave
    # the album's other tracks untouched.
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.scanner as scanner_mod

    d = tmp_path / "Artist" / "Album (2024)"
    d.mkdir(parents=True)
    grabbed = d / "03 - Grabbed.flac"
    grabbed.write_bytes(b"three")
    other = d / "01 - Other.flac"
    other.write_bytes(b"one")

    job = jm.Job(title="Grabbed", artist="Artist", album_id="alb9")
    job.status = jm.JobStatus.DONE
    job.single = {"album_id": "alb9", "track_id": "t3", "dir": str(d),
                  "isrc": "", "track_no": 3, "title": "Grabbed",
                  "artist": "Artist", "album": "Album",
                  "marked": True, "new_folder": False}
    jm.registry.add(job)
    monkeypatch.setattr(scanner_mod, "read_album_dir", lambda _d: [
        {"path": str(other), "isrc": "", "tracknumber": 1},
        {"path": str(grabbed), "isrc": "", "tracknumber": 3}])
    monkeypatch.setattr(beets_mod, "forget_beets_entries", lambda paths: len(paths))
    try:
        r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert r.status_code in (200, 303)
        assert not grabbed.exists()
        assert other.exists()
    finally:
        _remove_job(job)


def test_undo_with_no_isrc_or_track_number_deletes_nothing(
        client, monkeypatch, fresh_singles, tmp_path):
    # Neither an ISRC nor a track number to match on: two missing values must not
    # read as equal and delete an arbitrary track.
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.scanner as scanner_mod

    d = tmp_path / "Artist" / "Album (2024)"
    d.mkdir(parents=True)
    t = d / "01 - A.flac"
    t.write_bytes(b"flac")

    job = jm.Job(title="A", artist="Artist", album_id="alb8")
    job.status = jm.JobStatus.DONE
    job.single = {"album_id": "alb8", "track_id": "t1", "dir": str(d),
                  "isrc": "", "track_no": None, "title": "A",
                  "artist": "Artist", "album": "Album",
                  "marked": False, "new_folder": False}
    jm.registry.add(job)
    monkeypatch.setattr(scanner_mod, "read_album_dir", lambda _d: [
        {"path": str(t), "isrc": "", "tracknumber": 1}])
    monkeypatch.setattr(beets_mod, "forget_beets_entries", lambda paths: len(paths))
    try:
        client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert t.exists()
    finally:
        _remove_job(job)
