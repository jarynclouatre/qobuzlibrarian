"""Tests for quality/tiers.py and quality/decision.py."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from qobuz_fetch.quality.decision import (
    _track_quality_cmp,
    album_max_quality,
    compare_album_quality,
    existing_track_quality,
    is_album_capped,
    load_capped,
    mark_album_capped,
    quality_change_summary,
    save_capped,
)
from qobuz_fetch.quality.tiers import (
    format_quality,
    streamrip_quality_cap,
)


@pytest.fixture
def fresh_quality_cap():
    """Reset the streamrip cap cache so tests that read it see config, not
    state leaked from a previous case. Previous shape used setup_method /
    teardown_method which hid this precondition inside three classes."""
    import qobuz_fetch.quality.tiers as tiers_mod
    tiers_mod._streamrip_cap_cache = None
    yield
    tiers_mod._streamrip_cap_cache = None


@pytest.fixture
def primed_quality_cap_24_192():
    """Seed the cap cache with (24, 192000) so album_max_quality tests
    can exercise the cap logic without dragging in a streamrip subprocess."""
    import qobuz_fetch.quality.tiers as tiers_mod
    tiers_mod._streamrip_cap_cache = (24, 192000)
    yield
    tiers_mod._streamrip_cap_cache = None


class TestFormatQuality:
    def test_normal_cd(self):
        assert format_quality(16, 44100) == "16/44.1"

    def test_hires_96(self):
        assert format_quality(24, 96000) == "24/96"

    def test_zero_bits_returns_question(self):
        assert format_quality(0, 44100) == "?"


@pytest.mark.usefixtures("fresh_quality_cap")
class TestStreamripQualityCap:
    @pytest.mark.parametrize("quality,expected", [
        (4, (24, 192000)),
        ("3", (24, 96000)),
    ])
    def test_cap_for_quality(self, quality, expected):
        with patch("qobuz_fetch.config.STREAMRIP_QUALITY", quality):
            assert streamrip_quality_cap() == expected

    def test_result_is_cached(self):
        with patch("qobuz_fetch.config.STREAMRIP_QUALITY", 3):
            first = streamrip_quality_cap()
        with patch("qobuz_fetch.config.STREAMRIP_QUALITY", 4):
            second = streamrip_quality_cap()
        assert first == second == (24, 96000)


@pytest.mark.usefixtures("primed_quality_cap_24_192")
class TestAlbumMaxQuality:

    def test_khz_input_converted_to_hz(self):
        album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 96.0}
        bd, sr = album_max_quality(album)
        assert bd == 24 and sr == 96000

    def test_cap_applied_at_96khz(self):
        import qobuz_fetch.quality.tiers as tiers_mod
        tiers_mod._streamrip_cap_cache = (24, 96000)
        album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 192.0}
        bd, sr = album_max_quality(album)
        assert sr == 96000


class TestExistingTrackQuality:
    def test_returns_bits_and_rate(self):
        assert existing_track_quality({"bits": 24, "sample_rate": 96000}) == (24, 96000)


@pytest.mark.usefixtures("primed_quality_cap_24_192")
class TestCompareAlbumQuality:

    def _qalbum(self, bd=24, sr=96.0):
        return {"maximum_bit_depth": bd, "maximum_sampling_rate": sr}

    def test_no_existing_returns_no_existing(self):
        r = compare_album_quality([], self._qalbum())
        assert r["classification"] == "no_existing"

    def test_all_lower(self):
        tracks = [{"bits": 16, "sample_rate": 44100}]
        r = compare_album_quality(tracks, self._qalbum(bd=24, sr=96.0))
        assert r["classification"] == "all_lower"

    def test_all_equal(self):
        tracks = [{"bits": 24, "sample_rate": 96000}]
        r = compare_album_quality(tracks, self._qalbum(bd=24, sr=96.0))
        assert r["classification"] == "all_equal"

    def test_unreadable_track_surfaced_as_unknown(self):
        # A track whose quality couldn't be read must be counted (n_unknown), so
        # the upgrade path can refuse to wipe-replace it unverified.
        tracks = [{"bits": 16, "sample_rate": 44100}, {"bits": 0, "sample_rate": 0}]
        r = compare_album_quality(tracks, self._qalbum(bd=24, sr=96.0))
        assert r["n_unknown"] == 1


class TestTrackQualityCmp:
    def test_higher_quality_wins(self):
        assert _track_quality_cmp(
            {"bits": 24, "sample_rate": 96000},
            {"bits": 16, "sample_rate": 44100},
        ) == 1

    def test_equal_quality_is_a_tie(self):
        assert _track_quality_cmp(
            {"bits": 24, "sample_rate": 96000},
            {"bits": 24, "sample_rate": 96000},
        ) == 0

    def test_none_bits_does_not_crash(self):
        # A track dict carrying bits=None must not raise on comparison.
        assert _track_quality_cmp(
            {"bits": None, "sample_rate": 44100},
            {"bits": 24, "sample_rate": 96000},
        ) == -1


class TestQualityChangeSummary:
    def _t(self, bits, rate):
        return {"bits": bits, "sample_rate": rate}

    def test_all_upgrading(self):
        r = quality_change_summary([(self._t(16, 44100), self._t(24, 96000))])
        assert r["upgrading"] == 1

    def test_losing_hires(self):
        r = quality_change_summary([(self._t(24, 96000), self._t(16, 44100))])
        assert r["losing_hires"] == 1


class TestIsAlbumCapped:
    def _fresh(self, days_ago=0):
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        return {"ts": ts, "title": "Test Album"}

    def test_fresh_entry_returns_true(self):
        assert is_album_capped("123", {"123": self._fresh()}) is True

    def test_expired_entry_returns_false(self):
        assert is_album_capped("123", {"123": self._fresh(days_ago=91)}) is False

    def test_malformed_ts_returns_false(self):
        assert is_album_capped("123", {"123": {"ts": "not-a-date"}}) is False


class TestCappedPersistence:
    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("qobuz_fetch.config.CAPPED_FILE", tmp_path / "capped.json")
        ts = datetime.now(timezone.utc).isoformat()
        data = {"123": {"ts": ts, "title": "Alive"}}
        save_capped(data)
        assert load_capped() == {"123": {"ts": ts, "title": "Alive"}}

    def test_save_prunes_expired_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr("qobuz_fetch.config.CAPPED_FILE", tmp_path / "capped.json")
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        fresh_ts = datetime.now(timezone.utc).isoformat()
        save_capped({
            "old": {"ts": old_ts, "title": "Stale"},
            "new": {"ts": fresh_ts, "title": "Fresh"},
        })
        loaded = load_capped()
        assert "old" not in loaded
        assert "new" in loaded

    def test_load_returns_dict_when_file_is_a_json_list(self, tmp_path, monkeypatch):
        # A malformed capped file that parses as a list would otherwise reach
        # is_album_capped's `.get` and crash the upgrade scan.
        cfile = tmp_path / "capped.json"
        cfile.write_text('["x", "y"]', encoding="utf-8")
        monkeypatch.setattr("qobuz_fetch.config.CAPPED_FILE", cfile)
        assert load_capped() == {}
