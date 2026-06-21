"""Tests for qobuz_librarian.api.search — strict ISRC matching (album repair
depends on it), result extraction, pagination, and the album/catalog cache."""
from unittest.mock import patch

from qobuz_librarian.api.auth import QobuzError
from qobuz_librarian.api.search import (
    find_qobuz_track_by_isrc,
    get_artist_albums,
    search_albums,
    search_artists,
    search_tracks,
)


def _track(isrc=None, **kwargs):
    t = {"id": 123, "title": "Test Track", "duration": 200}
    if isrc is not None:
        t["isrc"] = isrc
    t.update(kwargs)
    return t


def test_find_qobuz_track_by_isrc_is_strict():
    # Hyphens/case are folded, but matching is otherwise exact — album repair
    # would refill the wrong recording if a substring/prefix counted.
    with patch("qobuz_librarian.api.search.search_tracks",
               return_value=[_track(isrc="USRC1234567")]):
        assert find_qobuz_track_by_isrc("US-RC1-23-4567", "tok")["isrc"] == "USRC1234567"
    for result_isrc in ("USRC12345678", "USRC1234567X"):  # extra digit / suffix
        with patch("qobuz_librarian.api.search.search_tracks",
                   return_value=[_track(isrc=result_isrc)]):
            assert find_qobuz_track_by_isrc("USRC1234567", "tok") is None
    # A track with no ISRC field is skipped, and the first exact match in
    # result order wins.
    ordered = [_track(isrc="OTHER12345", id=0), _track(isrc="USRC1234567", id=111),
               _track(isrc="USRC1234567", id=222)]
    with patch("qobuz_librarian.api.search.search_tracks", return_value=ordered):
        assert find_qobuz_track_by_isrc("USRC1234567", "tok")["id"] == 111


def test_find_qobuz_track_by_isrc_swallows_empty_and_errors():
    with patch("qobuz_librarian.api.search.search_tracks", return_value=[]):
        assert find_qobuz_track_by_isrc("USRC1234567", "tok") is None
    with patch("qobuz_librarian.api.search.search_tracks", side_effect=QobuzError("flaky")):
        assert find_qobuz_track_by_isrc("USRC1234567", "tok") is None


def test_search_helpers_extract_items_or_empty():
    with patch("qobuz_librarian.api.search.qobuz_get",
               return_value={"albums": {"items": [{"id": 1}, {"id": 2}]}}):
        assert search_albums("q", "tok") == [{"id": 1}, {"id": 2}]
    with patch("qobuz_librarian.api.search.qobuz_get",
               return_value={"tracks": {"items": [{"id": 1}]}}):
        assert search_tracks("q", "tok") == [{"id": 1}]
    with patch("qobuz_librarian.api.search.qobuz_get",
               return_value={"artists": {"items": [{"id": 1}]}}):
        assert search_artists("q", "tok") == [{"id": 1}]
    # A response missing the items envelope yields an empty list, not a KeyError.
    with patch("qobuz_librarian.api.search.qobuz_get", return_value={}):
        assert search_albums("q", "tok") == []


def test_search_guards_malformed_bodies():
    # A non-dict top-level body (a CDN/proxy error page served as a JSON list or
    # string) becomes the QobuzError callers already handle, not a raw .get crash.
    for bad in (["x"], "error", 42, None):
        with patch("qobuz_librarian.api.search.qobuz_get", return_value=bad):
            try:
                search_albums("q", "tok")
            except QobuzError:
                pass
            else:
                raise AssertionError(f"expected QobuzError for body {bad!r}")
    # A truthy but non-dict envelope (albums is a list/string) must yield [] via
    # the envelope guard rather than crashing on .get("items").
    with patch("qobuz_librarian.api.search.qobuz_get", return_value={"albums": ["x"]}):
        assert search_albums("q", "tok") == []
    with patch("qobuz_librarian.api.search.qobuz_get", return_value={"tracks": "oops"}):
        assert search_tracks("q", "tok") == []


def test_get_artist_albums_paginates_and_stops_early():
    page1 = {"albums": {"items": [{"id": i} for i in range(100)], "total": 105}}
    page2 = {"albums": {"items": [{"id": i} for i in range(100, 105)]}}
    page3 = {"albums": {"items": []}}
    with patch("qobuz_librarian.api.search.qobuz_get", side_effect=[page1, page2, page3]):
        items, total = get_artist_albums("artist123", "tok")
    assert len(items) == 105 and total == 105
    # A short first page (fewer than the page size) stops without a second call.
    short = {"albums": {"items": [{"id": i} for i in range(50)], "total": 200}}
    with patch("qobuz_librarian.api.search.qobuz_get", return_value=short):
        items, _ = get_artist_albums("artist123", "tok", limit=500)
    assert len(items) == 50


def test_get_artist_albums_does_not_cache_a_short_fetch(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.api import album_cache, search
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ALBUM_CACHE_ENABLED", True)
    monkeypatch.setattr(cfg, "ARTIST_CATALOG_CACHE_TTL", 3600)
    album_cache._reset_for_tests()
    try:
        # Qobuz says the artist has 105 albums but hands back 100 and then an
        # empty page — a transient short read. The 100 we got are returned for
        # this run, but caching them would hide the other 5 for the whole TTL,
        # so the next call must re-fetch rather than trust the truncated list.
        page1 = {"albums": {"items": [{"id": i} for i in range(100)], "total": 105}}
        empty = {"albums": {"items": []}}
        with patch("qobuz_librarian.api.search.qobuz_get", side_effect=[page1, empty]):
            items, total = search.get_artist_albums("ART_SHORT", "tok", limit=500)
        assert len(items) == 100 and total == 105
        full = {"albums": {"items": [{"id": i} for i in range(105)], "total": 105}}
        with patch("qobuz_librarian.api.search.qobuz_get", return_value=full) as gg:
            items2, _ = search.get_artist_albums("ART_SHORT", "tok", limit=500)
        assert gg.called and len(items2) == 105
    finally:
        album_cache._reset_for_tests()


def test_get_album_cached_by_id(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.api import album_cache, search
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ALBUM_CACHE_ENABLED", True)
    album_cache._reset_for_tests()
    try:
        calls = {"n": 0}

        def fake_get(endpoint, params, token):
            calls["n"] += 1
            return {"id": params["album_id"], "title": "X",
                    "tracks": {"items": [{"id": 1, "title": "T"}]}}

        monkeypatch.setattr(search, "qobuz_get", fake_get)
        # An album's track list is immutable → the second fetch is served from cache.
        a1 = search.get_album("ALB1", "tok")
        a2 = search.get_album("ALB1", "tok")
        assert calls["n"] == 1 and a1 == a2 and a1["id"] == "ALB1"
    finally:
        album_cache._reset_for_tests()


def test_get_album_does_not_cache_a_track_less_response(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.api import album_cache, search
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ALBUM_CACHE_ENABLED", True)
    album_cache._reset_for_tests()
    try:
        full = {"id": "ALB1", "title": "X",
                "tracks": {"items": [{"id": 1, "title": "T"}]}}
        # A transient/partial 200 with no tracks must not poison the TTL-less
        # cache — the next call re-fetches and gets the real track list.
        responses = [{"id": "ALB1", "title": "X"}, full]
        monkeypatch.setattr(search, "qobuz_get",
                            lambda *a, **k: responses.pop(0))
        first = search.get_album("ALB1", "tok")
        assert not (first.get("tracks") or {}).get("items")
        second = search.get_album("ALB1", "tok")
        assert (second.get("tracks") or {}).get("items")
    finally:
        album_cache._reset_for_tests()


def test_album_cache_trims_to_the_cap(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.api import album_cache
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ALBUM_CACHE_ENABLED", True)
    monkeypatch.setattr(album_cache, "_CACHE_MAX_ALBUMS", 3)
    album_cache._reset_for_tests()
    try:
        for aid in ("a", "b", "c", "d", "e"):
            album_cache.put(aid, {"id": aid})
        album_cache._trim_albums()
        survivors = [a for a in ("a", "b", "c", "d", "e") if album_cache.get(a)]
        assert len(survivors) == 3        # bounded at the cap
        assert album_cache.get("e")       # the most-recently-written survives
    finally:
        album_cache._reset_for_tests()


def test_album_cache_heals_on_corrupt_db(tmp_path, monkeypatch):
    # Data-page corruption (unclean power-off) can pass connect + CREATE TABLE
    # and only surface on a row read, leaving the cache permanently dead. A
    # corrupt read must discard the file and rebuild rather than swallow forever.
    import qobuz_librarian.config as cfg
    from qobuz_librarian.api import album_cache
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ALBUM_CACHE_ENABLED", True)
    album_cache._reset_for_tests()
    try:
        album_cache.put("123", {"id": "123"})
        assert album_cache.get("123") == {"id": "123"}

        db = tmp_path / "album_cache.db"
        assert db.exists()
        # Replace the on-disk db with a non-database: _ensure already passed
        # (init flag set), so the next access opens a fresh connection and the
        # SELECT raises "file is not a database".
        album_cache._reset_for_tests()
        album_cache._initialized = True
        db.write_bytes(b"not a sqlite database" * 200)

        assert album_cache.get("123") is None      # corrupt read -> heal
        assert album_cache._initialized is False    # forced rebuild next access

        # Cache works again and repopulates from scratch.
        album_cache.put("123", {"id": "123"})
        assert album_cache.get("123") == {"id": "123"}
    finally:
        album_cache._reset_for_tests()


def test_get_artist_albums_cached_within_ttl(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.api import album_cache, search
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ALBUM_CACHE_ENABLED", True)
    monkeypatch.setattr(cfg, "ARTIST_CATALOG_CACHE_TTL", 3600)
    album_cache._reset_for_tests()
    try:
        calls = {"n": 0}

        def fake_get(endpoint, params, token):
            calls["n"] += 1
            return {"albums": {"items": [{"id": "A1", "title": "X"}], "total": 1}}

        monkeypatch.setattr(search, "qobuz_get", fake_get)
        items1, total1 = search.get_artist_albums("ART1", "tok", limit=10)
        items2, total2 = search.get_artist_albums("ART1", "tok", limit=10)
        assert calls["n"] == 1 and total1 == total2 == 1
        assert [a["id"] for a in items1] == [a["id"] for a in items2] == ["A1"]
    finally:
        album_cache._reset_for_tests()


def test_album_cache_rebuilds_corrupt_db(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.api import album_cache
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ALBUM_CACHE_ENABLED", True)
    album_cache._reset_for_tests()
    # A truncated/garbage db file used to disable the cache for the whole
    # process; it should instead be discarded and rebuilt so caching resumes.
    (tmp_path / "album_cache.db").write_bytes(b"not a sqlite database, just junk")
    try:
        album_cache.put("ALB9", {"id": "ALB9", "title": "X"})
        assert album_cache.get("ALB9") == {"id": "ALB9", "title": "X"}
    finally:
        album_cache._reset_for_tests()
