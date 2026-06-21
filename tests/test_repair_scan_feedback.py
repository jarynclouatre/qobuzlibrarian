"""The whole-library repair sweep must give continuous proof-of-life.

A clean library logs nothing for minutes (only damaged albums print), and the
old code only ticked the progress header once per artist — so a long scan read
as a hang. These tests pin the two fixes: a per-album progress item and a
time-throttled "still scanning" heartbeat line.
"""
import logging

from qobuz_librarian.web import flows


class _FakeArtist:
    def __init__(self, name, albums):
        self.name = name
        self._albums = albums


class _FakeAlbum:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"/music/{self.name}"


class _RecordingJob:
    """Minimal stand-in for jobs.Job that records what the scan reports."""
    def __init__(self):
        self.cancel_requested = False
        self.candidates = []
        self.summary = ""
        self.progress_items = []   # the `item` of every push_progress call

    def push_progress(self, phase, current=0, total=0, item="", found=0, hit=None):
        self.progress_items.append(item)

    def push_line(self, line):
        pass

    def add_candidate(self, **kw):
        self.candidates.append(kw)
        return f"c{len(self.candidates)}"


def _wire(monkeypatch, artists, *, heartbeat_secs=0.0):
    """Point scan_repairs at a fake library that always scans clean."""
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "list_library_artists", lambda: artists)
    monkeypatch.setattr(flows, "list_artist_album_dirs", lambda ad: ad._albums)
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda mode: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda mode: None)
    monkeypatch.setattr(flows.time, "sleep", lambda *_a: None)
    # heartbeat_secs=0 → every album emits a beat, so the assertion is
    # deterministic without manipulating the clock.
    monkeypatch.setattr(flows, "_REPAIR_HEARTBEAT_SECS", heartbeat_secs)
    clean = {"verified_ok": 1, "unverified": 0, "verified_truncated": [],
             "no_isrc_tag": []}
    monkeypatch.setattr(
        "qobuz_librarian.repair_log.scan_dir_for_isrc_repairs",
        lambda album_dir, token, deep=False: clean)


def _capture_qobuz_log():
    records = []

    class _H(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())

    logger = logging.getLogger("qobuz_librarian")
    h = _H()
    logger.addHandler(h)
    prev = logger.level
    logger.setLevel(logging.DEBUG)
    return logger, h, prev, records


def test_repair_scan_reports_per_artist_progress(monkeypatch):
    artists = [
        _FakeArtist("Aretha Franklin",
                    [_FakeAlbum("Amazing Grace"), _FakeAlbum("Lady Soul"),
                     _FakeAlbum("Spirit in the Dark")]),
        _FakeArtist("Beyonce", [_FakeAlbum("Lemonade")]),
    ]
    _wire(monkeypatch, artists)
    job = _RecordingJob()

    flows.scan_repairs(job, "token")

    # The sweep fans artists out to workers but advances progress on the single
    # writer thread as each artist completes, so every artist name shows up in
    # the live "now checking" header.
    assert "Aretha Franklin" in job.progress_items
    assert "Beyonce" in job.progress_items
    # Clean library → nothing flagged.
    assert job.candidates == []


def test_repair_scan_emits_heartbeat_on_clean_library(monkeypatch):
    artists = [_FakeArtist(f"Artist {i}", [_FakeAlbum(f"Album {i}")])
               for i in range(5)]
    _wire(monkeypatch, artists)  # heartbeat every album
    logger, h, prev, records = _capture_qobuz_log()
    job = _RecordingJob()
    try:
        flows.scan_repairs(job, "token")
    finally:
        logger.removeHandler(h)
        logger.setLevel(prev)

    # The opening line sets expectations instead of going silent.
    assert any("healthy albums stay quiet" in m for m in records)
    # A clean library still produces visible "still scanning" proof-of-life.
    assert any("still scanning" in m for m in records)


def test_repair_scan_heartbeat_is_throttled(monkeypatch):
    # With a long heartbeat window and a fast (clean) scan, the loop should NOT
    # log a beat per album — the whole point of the time throttle.
    artists = [_FakeArtist(f"Artist {i}", [_FakeAlbum(f"Album {i}")])
               for i in range(8)]
    _wire(monkeypatch, artists, heartbeat_secs=3600)
    logger, h, prev, records = _capture_qobuz_log()
    job = _RecordingJob()
    try:
        flows.scan_repairs(job, "token")
    finally:
        logger.removeHandler(h)
        logger.setLevel(prev)

    beats = [m for m in records if "still scanning" in m]
    assert beats == []  # throttled out entirely within one fast pass


def test_repair_scan_verifies_every_track_deep(monkeypatch):
    # The whole-library sweep must call scan_dir_for_isrc_repairs(deep=True) so a
    # header-consistent truncation (decodes fine, STREAMINFO rewritten short, like
    # Jack's Mannequin / "Everything In Transit") is checked against its real
    # Qobuz length instead of being passed as ok. Regression guard for the
    # deep=False blind spot.
    seen_deep = []

    def fake_scan(album_dir, token, deep=False):
        seen_deep.append(deep)
        return {"verified_ok": 0, "unverified": 0, "no_isrc_tag": [],
                "verified_truncated": [{"title": "I'm Ready", "file_length": 3.2,
                                        "qobuz_duration": 235.0, "isrc": "X"}]}

    artists = [_FakeArtist("Jack's Mannequin",
                           [_FakeAlbum("Everything In Transit (2005)")])]
    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "list_library_artists", lambda: artists)
    monkeypatch.setattr(flows, "list_artist_album_dirs", lambda ad: ad._albums)
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda m: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda m: None)
    monkeypatch.setattr(flows.time, "sleep", lambda *_a: None)
    monkeypatch.setattr("qobuz_librarian.repair_log.scan_dir_for_isrc_repairs", fake_scan)

    job = _RecordingJob()
    flows.scan_repairs(job, "token")

    assert seen_deep == [True], f"sweep must use deep=True, got {seen_deep}"
    assert any("Everything In Transit" in c.get("title", "") for c in job.candidates)


def test_repair_cache_round_trips_and_invalidates_on_change(tmp_path):
    from qobuz_librarian.library import repair_cache
    album = tmp_path / "Some Album (2020)"
    album.mkdir()
    flac = album / "01 - Track.flac"
    flac.write_bytes(b"x" * 1000)

    sig = repair_cache.signature(album)
    assert sig  # a real album dir with audio yields a signature
    payload = {"verified_ok": 9, "specs": [{"kind": "repair", "title": "Some Album"}]}
    repair_cache.put(album, sig, payload)
    assert repair_cache.get(album, sig) == payload

    # Editing a file changes the signature, so the stale entry no longer matches
    # — the album re-scans rather than serving a cached result for changed files.
    flac.write_bytes(b"x" * 2000)
    sig2 = repair_cache.signature(album)
    assert sig2 and sig2 != sig
    assert repair_cache.get(album, sig2) is None


def test_repair_rescan_skips_unchanged_album(tmp_path, monkeypatch):
    # A real album dir so the cache signature is computable.
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"x" * 1000)
    artist_obj = _FakeArtist("Artist", [album])

    calls = []

    def fake_scan(album_dir, token, deep=False):
        calls.append(str(album_dir))
        return {"verified_ok": 1, "unverified": 0, "verified_truncated": [],
                "no_isrc_tag": []}

    monkeypatch.setattr(flows, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(flows, "list_library_artists", lambda: [artist_obj])
    monkeypatch.setattr(flows, "list_artist_album_dirs", lambda ad: ad._albums)
    monkeypatch.setattr(flows.scan_checkpoint, "load", lambda m: None)
    monkeypatch.setattr(flows.scan_checkpoint, "save", lambda *a, **k: None)
    monkeypatch.setattr(flows.scan_checkpoint, "clear", lambda m: None)
    monkeypatch.setattr("qobuz_librarian.repair_log.scan_dir_for_isrc_repairs", fake_scan)

    flows.scan_repairs(_RecordingJob(), "token")
    assert len(calls) == 1  # first scan does the real work
    flows.scan_repairs(_RecordingJob(), "token")
    assert len(calls) == 1  # unchanged album → served from cache, not re-scanned


def test_download_short_warns_advisory(monkeypatch):
    # A clean truncation decodes fine, so the download's decode gate can't see it;
    # the post-download verify must surface it as an advisory warning.
    monkeypatch.setattr(flows, "find_album_dir_filesystem", lambda album: "/music/A/Al")
    monkeypatch.setattr(flows, "_cached_album_outcome",
                        lambda d, n, t: {"specs": [{"kind": "repair", "title": "Al"}]})
    logger, h, prev, records = _capture_qobuz_log()
    try:
        flows._warn_if_download_short(object(), {"title": "Al"}, "A", "tok")
    finally:
        logger.removeHandler(h)
        logger.setLevel(prev)
    assert any("came up shorter" in m for m in records)


def test_download_short_silent_when_clean_or_unresolved(monkeypatch):
    monkeypatch.setattr(flows, "_cached_album_outcome", lambda d, n, t: {"specs": []})
    monkeypatch.setattr(flows, "find_album_dir_filesystem", lambda album: "/music/A/Al")
    logger, h, prev, records = _capture_qobuz_log()
    try:
        flows._warn_if_download_short(object(), {"title": "Al"}, "A", "tok")
        # Unresolvable dir → silently skipped, never raises.
        monkeypatch.setattr(flows, "find_album_dir_filesystem", lambda album: None)
        flows._warn_if_download_short(object(), {"title": "Al"}, "A", "tok")
    finally:
        logger.removeHandler(h)
        logger.setLevel(prev)
    assert not any("came up shorter" in m for m in records)
