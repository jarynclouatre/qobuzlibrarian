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


def test_undo_no_isrc_removes_the_grabbed_disc_not_a_same_numbered_twin(
        client, monkeypatch, fresh_singles, tmp_path):
    # Grab a no-ISRC track that lives on CD2 of a multi-disc album where CD1 has
    # a track with the SAME per-disc number. Undo must delete the CD2 track the
    # grab added — never CD1's same-numbered file. The grab records its disc so
    # the number-only fallback can't pick the wrong disc.
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.library.scanner as scanner_mod
    import qobuz_librarian.queue.executor as ex_mod
    import qobuz_librarian.web.app as app_mod

    d = tmp_path / "Artist" / "Box Set (2020)"
    cd1 = d / "CD1"
    cd2 = d / "CD2"
    cd1.mkdir(parents=True)
    cd2.mkdir(parents=True)
    cd1_twin = cd1 / "03 - Disc One Three.flac"
    cd2_grabbed = cd2 / "03 - Disc Two Three.flac"
    cd1_twin.write_bytes(b"cd1")
    cd2_grabbed.write_bytes(b"cd2")

    monkeypatch.setattr(app_mod, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "albx", "title": "Box Set", "year": 2020,
        "artist": {"name": "Artist"},
        "tracks": {"items": [
            {"id": "t_a", "title": "A", "track_number": 1, "media_number": 1},
            {"id": "t_b", "title": "B", "track_number": 2, "media_number": 1},
            {"id": "cd2t3", "title": "Disc Two Three",
             "track_number": 3, "media_number": 2}]}})
    monkeypatch.setattr(cat_mod, "find_existing_tracks", lambda *a, **k: ([], None))

    def fake_exec(queue, *a, **k):
        queue[0]["n_ok"] = 1
        queue[0]["imported"] = True
        queue[0]["n_fail"] = 0
        queue[0]["_resolved_post_dir"] = str(d)
    monkeypatch.setattr(ex_mod, "_execute_download_queue", fake_exec)

    jm.start_worker()
    r = client.post("/download", data={"album_id": "albx", "track_id": "cd2t3"},
                    follow_redirects=False)
    assert r.status_code in (200, 303)
    job = [j for j in list(jm.registry._jobs.values())
           if getattr(j, "album_id", None) == "albx"][0]
    try:
        assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        assert job.status == jm.JobStatus.DONE
        assert job.single.get("disc_no") == 2

        monkeypatch.setattr(scanner_mod, "read_album_dir", lambda _d: [
            {"path": str(cd1_twin), "isrc": "", "tracknumber": 3, "discnumber": 1},
            {"path": str(cd2_grabbed), "isrc": "", "tracknumber": 3, "discnumber": 2}])
        monkeypatch.setattr(beets_mod, "forget_beets_entries", lambda paths: len(paths))
        client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert not cd2_grabbed.exists()
        assert cd1_twin.exists()
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
