"""The whole-library repair sweep must give continuous proof-of-life.

A clean library logs nothing for minutes (only damaged albums print), so the
sweep advances the progress header per artist and logs a time-throttled "still
scanning" heartbeat — emitted by whichever worker crosses the interval, so it
keeps ticking even while every worker is deep inside one large artist. These
tests pin that feedback, plus the ISRC-lookup cache and the post-download
length recheck.
"""
import logging

from qobuz_librarian import config as cfg
from qobuz_librarian import repair_log
from qobuz_librarian.library import repair_cache
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
    #
    # This mocks scan_dir_for_isrc_repairs, so it pins that the sweep passes
    # deep=True and turns a truncation into a candidate; the duration comparison
    # itself is exercised in test_repair_accuracy.py.
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


def test_isrc_lookup_cache_round_trips_and_skips_misses(monkeypatch):
    monkeypatch.setattr(cfg, "REPAIR_CACHE_ENABLED", True)
    repair_cache._reset_for_tests()
    try:
        track = {"id": 42, "duration": 235, "isrc": "USRC11900001"}
        assert repair_cache.get_track("USRC11900001") is None  # cold
        repair_cache.put_track("USRC11900001", track)
        assert repair_cache.get_track("USRC11900001") == track
        # A miss (None/empty) is never stored, so a transient outage can't get
        # frozen as a stable "no match" for the whole TTL.
        repair_cache.put_track("USRC11900002", None)
        repair_cache.put_track("USRC11900003", {})
        assert repair_cache.get_track("USRC11900002") is None
        assert repair_cache.get_track("USRC11900003") is None
        # An entry past the TTL re-verifies (returns None) rather than serving stale.
        monkeypatch.setattr(cfg, "REPAIR_CACHE_TTL_DAYS", 1)
        monkeypatch.setattr(repair_cache.time, "time", lambda: 9_999_999_999.0)
        assert repair_cache.get_track("USRC11900001") is None
    finally:
        repair_cache._reset_for_tests()


def test_isrc_lookup_is_cached_not_re_fetched(monkeypatch):
    # The scan re-decodes every file each run, but the Qobuz ISRC lookup is
    # cached so a re-scan — and any album sharing the ISRC — skips the network.
    monkeypatch.setattr(cfg, "REPAIR_CACHE_ENABLED", True)
    repair_cache._reset_for_tests()
    calls = []

    def fake_lookup(isrc, token):
        calls.append(isrc)
        return {"id": 1, "duration": 200, "isrc": isrc}

    monkeypatch.setattr(repair_log, "find_qobuz_track_by_isrc", fake_lookup)
    try:
        first = repair_log._qobuz_track_by_isrc("USRC1", "tok")
        second = repair_log._qobuz_track_by_isrc("USRC1", "tok")
        assert first == second
        assert calls == ["USRC1"]  # second call served from cache
    finally:
        repair_cache._reset_for_tests()


def test_truncated_after_download_returns_short_and_swallows_errors(monkeypatch):
    # The shared post-download recheck (used by every download path) returns the
    # tracks that came up short, and a verify hiccup never fails the download.
    monkeypatch.setattr(repair_log, "scan_dir_for_isrc_repairs",
                        lambda album_dir, token, deep=True:
                        {"verified_truncated": [{"title": "X"}]})
    assert repair_log.truncated_tracks_after_download("/music/A/Al", "tok") == [{"title": "X"}]

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(repair_log, "scan_dir_for_isrc_repairs", boom)
    assert repair_log.truncated_tracks_after_download("/music/A/Al", "tok") == []


def test_persisted_quality_tier_1_coerces_to_lossless(monkeypatch):
    # A settings.json from before the lossy tiers were dropped can still carry
    # STREAMRIP_QUALITY=1. The load path must honour it as CD lossless (tier 2),
    # not let it fail the choices check and revert to the default (tier 4, the
    # largest files) — the opposite of the "smaller files" the user picked.
    from qobuz_librarian.web import settings_store
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    settings_store._apply({"STREAMRIP_QUALITY": "1"})
    assert cfg.STREAMRIP_QUALITY == 2
