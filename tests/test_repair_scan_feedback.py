"""The whole-library repair sweep must give continuous proof-of-life.

A clean library logs nothing for minutes (only damaged albums print), so the
sweep keeps an in-place progress line ticking — refreshed by whichever worker
crosses the interval, so it stays alive even while every worker is deep inside
one large artist. These tests pin that feedback, plus the ISRC-lookup cache and
the post-download length recheck.
"""
import logging

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import repair_log
from qobuz_librarian.api.auth import AuthLost, QobuzUnavailable
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

    # Workers refresh the live progress line with the artist they're scanning, so
    # each artist's name shows up in the in-place status as the sweep runs.
    assert any("Aretha Franklin" in it for it in job.progress_items)
    assert any("Beyonce" in it for it in job.progress_items)
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
    # A clean library still ticks a visible live status — in the progress line,
    # not the log — so the scan never reads as hung.
    assert any(it.startswith('"') for it in job.progress_items)


def test_repair_scan_heartbeat_is_throttled(monkeypatch):
    # With a long heartbeat window and a fast (clean) scan, no mid-artist beat
    # should fire per album — the whole point of the time throttle. (Per-artist
    # completion still refreshes the line; only the heartbeat is gated.)
    artists = [_FakeArtist(f"Artist {i}", [_FakeAlbum(f"Album {i}")])
               for i in range(8)]
    _wire(monkeypatch, artists, heartbeat_secs=3600)
    job = _RecordingJob()
    flows.scan_repairs(job, "token")

    beats = [it for it in job.progress_items if it.startswith('"')]
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
    # truncated_tracks_after_download returns the tracks that came up short, and
    # a verify hiccup returns [] so a finished download is never failed by it.
    monkeypatch.setattr(repair_log, "scan_dir_for_isrc_repairs",
                        lambda album_dir, token, deep=True:
                        {"verified_truncated": [{"title": "X"}]})
    assert repair_log.truncated_tracks_after_download("/music/A/Al", "tok") == [{"title": "X"}]

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(repair_log, "scan_dir_for_isrc_repairs", boom)
    assert repair_log.truncated_tracks_after_download("/music/A/Al", "tok") == []


def test_persisted_quality_tier_1_coerces_to_lossless(monkeypatch):
    # A persisted STREAMRIP_QUALITY=1 (a lossy MP3 tier the FLAC-only pipeline
    # can't keep) must load as CD lossless (tier 2), not fail the choices check
    # and revert to the default (tier 4, the largest files) — the opposite of
    # the "smaller files" a tier-1 pick intends.
    from qobuz_librarian.web import settings_store
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    settings_store._apply({"STREAMRIP_QUALITY": "1"})
    assert cfg.STREAMRIP_QUALITY == 2


def test_persisted_quality_tier_1_rewritten_out_of_settings_file(tmp_path, monkeypatch):
    # load() doesn't just coerce a stale lossy tier in cfg — it normalises it on
    # disk so the value doesn't linger and re-coerce on every startup.
    import json

    from qobuz_librarian.web import settings_store
    sfile = tmp_path / "settings.json"
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", sfile)
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    sfile.write_text(json.dumps({"STREAMRIP_QUALITY": "1"}), encoding="utf-8")
    settings_store.load()
    assert json.loads(sfile.read_text(encoding="utf-8"))["STREAMRIP_QUALITY"] == "2"


def test_truncated_after_download_propagates_abort_signals(monkeypatch):
    # AuthLost / QobuzUnavailable mean the lengths weren't actually verified, so
    # they must propagate rather than be swallowed as "[] → all fine" — otherwise
    # a token that dies mid-recheck hides itself and the album reads as clean.
    for exc in (AuthLost, QobuzUnavailable):
        def boom(*a, _e=exc, **k):
            raise _e("x")
        monkeypatch.setattr(repair_log, "scan_dir_for_isrc_repairs", boom)
        with pytest.raises(exc):
            repair_log.truncated_tracks_after_download("/music/A/Al", "tok")


def test_warn_if_download_truncated_surfaces_auth_loss_without_raising(monkeypatch, caplog):
    # The user-facing recheck wrapper turns an abort signal into a clear "couldn't
    # verify" log and returns [] — it must never crash a finished download, but it
    # must not silently report a clean bill of health either.
    def boom(*a, **k):
        raise AuthLost("token gone")
    monkeypatch.setattr(repair_log, "scan_dir_for_isrc_repairs", boom)
    with caplog.at_level(logging.INFO):
        assert repair_log.warn_if_download_truncated("/music/A/Al", "tok", "Album") == []
    assert "couldn't be verified" in caplog.text.lower()


def test_prune_expired_drops_stale_keeps_fresh_and_throttles(monkeypatch, tmp_path):
    import json
    import sqlite3
    import time as _time
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "REPAIR_CACHE_ENABLED", True)
    monkeypatch.setattr(cfg, "REPAIR_CACHE_TTL_DAYS", 30)
    repair_cache._reset_for_tests()
    try:
        assert repair_cache._ensure()
        now = int(_time.time())
        conn = repair_cache._conn()
        conn.execute(
            "INSERT OR REPLACE INTO tracks (isrc, stored_at, payload) VALUES (?,?,?)",
            ("USOLD0000001", now - 40 * 86400, json.dumps({"id": 1})))
        conn.commit()
        repair_cache.put_track("USNEW0000001", {"id": 2, "isrc": "USNEW0000001"})

        assert repair_cache.prune_expired(force=True) == 1   # only the stale row
        with sqlite3.connect(str(tmp_path / "repair_cache.db")) as c:
            rows = {r[0] for r in c.execute("SELECT isrc FROM tracks")}
        assert rows == {"USNEW0000001"}

        # A second stale row plus a non-force prune: the daily throttle (the force
        # run above just stamped it) skips the walk, so the stale row survives.
        conn = repair_cache._conn()
        conn.execute(
            "INSERT OR REPLACE INTO tracks (isrc, stored_at, payload) VALUES (?,?,?)",
            ("USOLD0000002", now - 40 * 86400, json.dumps({"id": 3})))
        conn.commit()
        assert repair_cache.prune_expired() == 0             # throttled
        with sqlite3.connect(str(tmp_path / "repair_cache.db")) as c:
            rows = {r[0] for r in c.execute("SELECT isrc FROM tracks")}
        assert "USOLD0000002" in rows

        # TTL of 0 means keep-forever: even forced, nothing is pruned.
        monkeypatch.setattr(cfg, "REPAIR_CACHE_TTL_DAYS", 0)
        assert repair_cache.prune_expired(force=True) == 0
    finally:
        repair_cache._reset_for_tests()


def test_repair_cache_heals_on_corrupt_db(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "REPAIR_CACHE_ENABLED", True)
    repair_cache._reset_for_tests()
    try:
        repair_cache.put_track("USRC0000001", {"id": 1, "isrc": "USRC0000001"})
        assert repair_cache.get_track("USRC0000001") == {"id": 1, "isrc": "USRC0000001"}
        gen0 = repair_cache._generation
        # Corrupt the db on disk and drop this thread's open handle so the next
        # op reopens against the malformed file.
        (tmp_path / "repair_cache.db").write_bytes(b"not a sqlite database, junk")
        repair_cache._local.conn = None
        # A read against the corrupt file heals (returns None, no raise) and bumps
        # the generation so other workers reopen against the rebuilt db.
        assert repair_cache.get_track("USRC0000001") is None
        assert repair_cache._generation == gen0 + 1
        # The cache rebuilds transparently on the next write.
        repair_cache.put_track("USRC0000002", {"id": 2, "isrc": "USRC0000002"})
        assert repair_cache.get_track("USRC0000002") == {"id": 2, "isrc": "USRC0000002"}
    finally:
        repair_cache._reset_for_tests()


def test_repair_cache_conn_reopens_after_generation_bump(monkeypatch, tmp_path):
    # A worker mid-scan must stop writing into a db another worker discarded: a
    # bumped generation forces _conn() to drop and replace this thread's handle.
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "REPAIR_CACHE_ENABLED", True)
    repair_cache._reset_for_tests()
    try:
        assert repair_cache._ensure()
        c1 = repair_cache._conn()
        assert repair_cache._local.generation == repair_cache._generation
        repair_cache._generation += 1            # simulate another worker's recovery
        c2 = repair_cache._conn()
        assert c2 is not c1
        assert repair_cache._local.generation == repair_cache._generation
    finally:
        repair_cache._reset_for_tests()


def test_scan_dir_caches_isrc_lookups_across_runs(monkeypatch, tmp_path):
    # End to end: with the cache on, a second scan_dir_for_isrc_repairs over the
    # same album issues ZERO Qobuz ISRC lookups (served from cache) while still
    # re-running the local decode probe on every file each time.
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "REPAIR_CACHE_ENABLED", True)
    repair_cache._reset_for_tests()
    calls = []

    def fake_lookup(isrc, token):
        calls.append(isrc)
        return {"duration": 200.0, "title": "t", "track_number": 1, "isrc": isrc}

    decode_calls = []

    def fake_decode(path):
        decode_calls.append(path)
        return True

    entries = [{"path": "/nonexistent/01.flac", "title": "t",
                "isrc": "USABC1234500", "length": 200.0,
                "sample_rate": 44100, "bits": 16, "channels": 2}]
    monkeypatch.setattr(repair_log, "find_qobuz_track_by_isrc", fake_lookup)
    monkeypatch.setattr(repair_log, "read_album_dir", lambda d: entries)
    monkeypatch.setattr(repair_log, "_flac_decode_ok", fake_decode)
    try:
        repair_log.scan_dir_for_isrc_repairs("/album", "tok", deep=True)
        repair_log.scan_dir_for_isrc_repairs("/album", "tok", deep=True)
        assert calls == ["USABC1234500"]        # second scan hit the cache
    finally:
        repair_cache._reset_for_tests()
