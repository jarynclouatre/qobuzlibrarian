"""Tests for quality/tiers.py and quality/decision.py."""
from datetime import datetime, timedelta, timezone

from qobuz_librarian.quality.decision import (
    _track_quality_cmp,
    album_max_quality,
    compare_album_quality,
    is_album_capped,
    load_capped,
    quality_change_summary,
    save_capped,
)
from qobuz_librarian.quality.tiers import format_quality, streamrip_quality_cap


def test_format_quality_renders_known_tiers_and_unknown():
    assert format_quality(16, 44100) == "16/44.1"
    assert format_quality(24, 96000) == "24/96"
    # A 0-bit track read came back unreadable — show "?" instead of crashing.
    assert format_quality(0, 44100) == "?"


def test_streamrip_quality_cap_tracks_the_current_tier(monkeypatch):
    # CD lossless and full hi-res cap to their tier's ceiling.
    monkeypatch.setattr("qobuz_librarian.config.STREAMRIP_QUALITY", 2)
    assert streamrip_quality_cap() == (16, 44100)
    # Reads live, so a quality change (Settings page) takes effect at once
    # rather than reasoning at the value in effect on first call.
    monkeypatch.setattr("qobuz_librarian.config.STREAMRIP_QUALITY", 4)
    assert streamrip_quality_cap() == (24, 192000)


def test_album_max_quality_normalises_khz_and_applies_cap(monkeypatch):
    monkeypatch.setattr("qobuz_librarian.config.DOWNSAMPLE_HIRES_ENABLED", False)
    monkeypatch.setattr("qobuz_librarian.config.STREAMRIP_QUALITY", 3)
    # The Qobuz API reports sample rate in kHz floats — store as Hz int.
    assert album_max_quality(
        {"maximum_bit_depth": 24, "maximum_sampling_rate": 96.0}) == (24, 96000)
    # A 192 album under a 96 cap caps down to 96 — we won't claim to fetch
    # higher than what streamrip will actually download.
    assert album_max_quality(
        {"maximum_bit_depth": 24, "maximum_sampling_rate": 192.0})[1] == 96000


def test_album_max_quality_reflects_downsample_target(monkeypatch):
    # With downsampling on, a 24/192 master lands on disk as 24/48. The
    # comparison target must match that, or the album reads as below target
    # and gets re-ripped on every scan forever.
    monkeypatch.setattr("qobuz_librarian.config.STREAMRIP_QUALITY", 4)
    monkeypatch.setattr("qobuz_librarian.config.DOWNSAMPLE_HIRES_ENABLED", True)
    monkeypatch.setattr("qobuz_librarian.quality.decision.HAVE_DOWNSAMPLE", True)
    assert album_max_quality(
        {"maximum_bit_depth": 24, "maximum_sampling_rate": 192.0}) == (24, 48000)
    assert album_max_quality(
        {"maximum_bit_depth": 24, "maximum_sampling_rate": 88.2}) == (24, 44100)
    # 44.1/48 kHz aren't resampled, so they pass through.
    assert album_max_quality(
        {"maximum_bit_depth": 16, "maximum_sampling_rate": 44.1}) == (16, 44100)


def test_compare_album_quality_classifies_and_counts_unknown():
    qalbum = {"maximum_bit_depth": 24, "maximum_sampling_rate": 96.0}
    # No existing tracks — there's nothing to compare, classification stays distinct.
    assert compare_album_quality([], qalbum)["classification"] == "no_existing"
    # All-lower → all-upgrading territory.
    assert compare_album_quality(
        [{"bits": 16, "sample_rate": 44100}], qalbum)["classification"] == "all_lower"
    # All-equal — nothing to do, but must classify distinctly so the upgrade
    # flow doesn't kick off a wipe-replace for parity.
    assert compare_album_quality(
        [{"bits": 24, "sample_rate": 96000}], qalbum)["classification"] == "all_equal"
    # An unreadable track (bits=0) gets surfaced as n_unknown so the upgrade
    # path won't wipe-replace it unverified.
    r = compare_album_quality(
        [{"bits": 16, "sample_rate": 44100}, {"bits": 0, "sample_rate": 0}], qalbum)
    assert r["n_unknown"] == 1


def test_track_quality_cmp_handles_none_bits():
    # A None bits/rate (from a tag read that failed mid-scan) must sort as
    # lower, not crash the comparison.
    assert _track_quality_cmp(
        {"bits": 24, "sample_rate": 96000}, {"bits": 16, "sample_rate": 44100}) == 1
    assert _track_quality_cmp(
        {"bits": 24, "sample_rate": 96000}, {"bits": 24, "sample_rate": 96000}) == 0
    assert _track_quality_cmp(
        {"bits": None, "sample_rate": 44100}, {"bits": 24, "sample_rate": 96000}) == -1


def test_quality_change_summary_counts_upgrades_and_losses():
    t = lambda b, r: {"bits": b, "sample_rate": r}
    assert quality_change_summary([(t(16, 44100), t(24, 96000))])["upgrading"] == 1
    # A would-be downgrade from hi-res must be flagged so we can refuse it.
    assert quality_change_summary([(t(24, 96000), t(16, 44100))])["losing_hires"] == 1


def test_is_album_capped_honours_ttl_and_tolerates_garbage():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    expired_ts = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    assert is_album_capped("123", {"123": {"ts": fresh_ts, "title": "T"}}) is True
    assert is_album_capped("123", {"123": {"ts": expired_ts, "title": "T"}}) is False
    # A garbage timestamp must not crash the upgrade scan — fall through to False.
    assert is_album_capped("123", {"123": {"ts": "not-a-date"}}) is False


def test_capped_persistence_round_trips_and_prunes(tmp_path, monkeypatch):
    monkeypatch.setattr("qobuz_librarian.config.CAPPED_FILE", tmp_path / "capped.json")
    fresh = datetime.now(timezone.utc).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    save_capped({"old": {"ts": stale, "title": "Stale"},
                 "new": {"ts": fresh, "title": "Fresh"}})
    loaded = load_capped()
    assert loaded == {"new": {"ts": fresh, "title": "Fresh"}}

    # If someone hand-edits the file into a JSON list, load_capped must return
    # a dict so is_album_capped's .get() doesn't blow up the upgrade scan.
    (tmp_path / "capped.json").write_text('["x", "y"]', encoding="utf-8")
    assert load_capped() == {}
