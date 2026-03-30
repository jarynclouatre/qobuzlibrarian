"""Tests for qobuz_librarian.api.search

find_qobuz_track_by_isrc required coverage: mode 6 (album repair) depends
on strict ISRC matching, not fuzzy/substring.
"""
from unittest.mock import patch

import pytest

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


class TestFindQobuzTrackByIsrc:
    """ISRC matching is case-folded and hyphen-stripped but otherwise STRICT."""

    def test_hyphenated_input_matches_unhyphenated_result(self):
        results = [_track(isrc="USRC1234567")]
        with patch("qobuz_librarian.api.search.search_tracks", return_value=results):
            r = find_qobuz_track_by_isrc("US-RC1-23-4567", "tok")
        assert r is results[0]

    def test_substring_match_does_not_count(self):
        # Track ISRC has an extra digit — must not match
        results = [_track(isrc="USRC12345678")]
        with patch("qobuz_librarian.api.search.search_tracks", return_value=results):
            assert find_qobuz_track_by_isrc("USRC1234567", "tok") is None

    def test_prefix_match_does_not_count(self):
        results = [_track(isrc="USRC1234567X")]
        with patch("qobuz_librarian.api.search.search_tracks", return_value=results):
            assert find_qobuz_track_by_isrc("USRC1234567", "tok") is None

    def test_track_with_no_isrc_field_skipped(self):
        results = [_track()]
        with patch("qobuz_librarian.api.search.search_tracks", return_value=results):
            assert find_qobuz_track_by_isrc("USRC1234567", "tok") is None

    def test_returns_first_match_in_result_order(self):
        results = [
            _track(isrc="OTHER12345", id=0),
            _track(isrc="USRC1234567", id=111),
            _track(isrc="USRC1234567", id=222),
        ]
        with patch("qobuz_librarian.api.search.search_tracks", return_value=results):
            r = find_qobuz_track_by_isrc("USRC1234567", "tok")
        assert r["id"] == 111

    def test_empty_results_returns_none(self):
        with patch("qobuz_librarian.api.search.search_tracks", return_value=[]):
            assert find_qobuz_track_by_isrc("USRC1234567", "tok") is None

    def test_qobuz_error_returns_none(self):
        with patch("qobuz_librarian.api.search.search_tracks",
                   side_effect=QobuzError("flaky")):
            assert find_qobuz_track_by_isrc("USRC1234567", "tok") is None


class TestSearchAlbums:
    def test_extracts_items(self):
        response = {"albums": {"items": [{"id": 1}, {"id": 2}]}}
        with patch("qobuz_librarian.api.search.qobuz_get", return_value=response):
            assert search_albums("test", "tok") == [{"id": 1}, {"id": 2}]

    def test_returns_empty_list_when_no_items(self):
        with patch("qobuz_librarian.api.search.qobuz_get", return_value={}):
            assert search_albums("test", "tok") == []


class TestSearchTracks:
    def test_extracts_items(self):
        response = {"tracks": {"items": [{"id": 1}]}}
        with patch("qobuz_librarian.api.search.qobuz_get", return_value=response):
            assert search_tracks("test", "tok") == [{"id": 1}]


class TestSearchArtists:
    def test_extracts_items(self):
        response = {"artists": {"items": [{"id": 1}]}}
        with patch("qobuz_librarian.api.search.qobuz_get", return_value=response):
            assert search_artists("test", "tok") == [{"id": 1}]


class TestGetArtistAlbums:
    def test_aggregates_pages(self):
        page1 = {"albums": {"items": [{"id": i} for i in range(100)], "total": 105}}
        page2 = {"albums": {"items": [{"id": i} for i in range(100, 105)]}}
        page3 = {"albums": {"items": []}}
        with patch("qobuz_librarian.api.search.qobuz_get",
                   side_effect=[page1, page2, page3]):
            items, total = get_artist_albums("artist123", "tok")
        assert len(items) == 105
        assert total == 105

    def test_short_page_stops_early(self):
        page1 = {"albums": {"items": [{"id": i} for i in range(50)], "total": 200}}
        with patch("qobuz_librarian.api.search.qobuz_get", return_value=page1):
            items, _ = get_artist_albums("artist123", "tok", limit=500)
        assert len(items) == 50


def test_get_album_caches_by_id_across_calls(tmp_path, monkeypatch):
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
        a1 = search.get_album("ALB1", "tok")
        a2 = search.get_album("ALB1", "tok")     # immutable → served from cache
        assert calls["n"] == 1
        assert a1 == a2 and a1["id"] == "ALB1"
    finally:
        album_cache._reset_for_tests()
