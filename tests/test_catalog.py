"""Tests for compute_missing and catalog helpers."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from qobuz_fetch.library.catalog import (
    _has_separator_match,
    _is_migration_candidate,
    _is_multi_artist_subset,
    _is_split_album_merge,
    _paths_equal,
    album_quality_label,
    album_year,
    album_year_int,
    compute_missing,
    dedup_album_versions,
    dedup_albums,
    filter_compilation_albums,
    filter_owned_albums,
    filter_seen_album_ids,
    filter_short_releases,
    find_extras_in_existing,
    is_lossless_album,
)


def _qt(title, isrc="", mbid="", disc=1, **kw):
    return {"title": title, "isrc": isrc, "mbid": mbid, "media_number": disc, **kw}


def _et(title, isrc="", mb_trackid="", disc=1, **kw):
    from qobuz_fetch.library.tags import normalize
    return {"title": title, "isrc": isrc, "mb_trackid": mb_trackid,
            "discnumber": disc, "normalized": normalize(title), **kw}


class TestComputeMissing:
    def test_layer1_isrc_match(self):
        qobuz = [_qt("Song A", isrc="USRC1234567")]
        existing = [_et("Completely Different Title", isrc="USRC1234567")]
        missing, present = compute_missing(qobuz, existing)
        assert len(present) == 1 and len(missing) == 0

    def test_layer3_disc_title_different_disc(self):
        qobuz = [_qt("Intro", disc=2)]
        existing = [_et("Intro", disc=1)]
        missing, present = compute_missing(qobuz, existing)
        assert len(missing) == 1 and len(present) == 0

    def test_layer4a_qobuz_stripped_matches_existing(self):
        qobuz = [_qt("Song (2014 Remaster)", disc=1)]
        existing = [_et("Song", disc=1)]
        missing, present = compute_missing(qobuz, existing)
        assert len(present) == 1 and len(missing) == 0

    def test_performance_variant_not_stripped(self):
        qobuz = [_qt("Song", disc=1)]
        existing = [_et("Song (Acoustic)", disc=1)]
        missing, present = compute_missing(qobuz, existing)
        assert len(missing) == 1 and len(present) == 0

    def test_genuinely_missing_track(self):
        qobuz = [_qt("Missing Song", isrc="AAA111", mbid="aaa")]
        existing = [_et("Other Song", isrc="BBB222", mb_trackid="bbb")]
        missing, present = compute_missing(qobuz, existing)
        assert len(missing) == 1 and len(present) == 0

    def test_repeated_title_one_file_covers_only_one(self):
        # An EP listing the same title twice (a reprise / alternate version):
        # one file on disk satisfies exactly one of them, not both.
        qobuz = [_qt("Dayvan Cowboy", disc=1), _qt("Dayvan Cowboy", disc=1)]
        existing = [_et("Dayvan Cowboy", disc=1)]
        missing, present = compute_missing(qobuz, existing)
        assert len(present) == 1 and len(missing) == 1

    def test_stripped_duplicate_not_double_counted(self):
        # "Time" on disk must not also satisfy "Time (Bonus Track)".
        qobuz = [_qt("Time", disc=1), _qt("Time (Bonus Track)", disc=1)]
        existing = [_et("Time", disc=1)]
        missing, present = compute_missing(qobuz, existing)
        assert [t["title"] for t in present] == ["Time"]
        assert [t["title"] for t in missing] == ["Time (Bonus Track)"]

    def test_whitespace_isrc_is_not_a_shared_identity(self):
        # A blank/whitespace ISRC tag must not pair two unrelated tracks.
        qobuz = [_qt("Track One", isrc="   ")]
        existing = [_et("Track Two", isrc="   ")]
        missing, present = compute_missing(qobuz, existing)
        assert len(present) == 0 and len(missing) == 1


class TestFindExtrasInExisting:
    def test_no_extras_when_all_match(self):
        qobuz = [_qt("Song A", isrc="USRC1234567")]
        existing = [_et("Song A", isrc="USRC1234567")]
        assert find_extras_in_existing(qobuz, existing) == []

    def test_extra_track_not_in_qobuz(self):
        qobuz = [_qt("Song A", isrc="USRC1234567")]
        existing = [_et("Song A", isrc="USRC1234567"), _et("Bonus Track", isrc="BONUS999")]
        extras = find_extras_in_existing(qobuz, existing)
        assert len(extras) == 1

    def test_same_title_bonus_is_flagged_extra(self):
        # Qobuz lists one "Time"; disk has it plus a same-stripped bonus. The
        # bonus must be an extra so an upgrade wipe-replace can't delete it.
        qobuz = [_qt("Time", disc=1)]
        existing = [_et("Time", disc=1), _et("Time (Bonus Track)", disc=1)]
        extras = find_extras_in_existing(qobuz, existing)
        assert len(extras) == 1 and "Bonus" in extras[0]["title"]

    def test_whitespace_isrc_does_not_hide_a_bonus(self):
        # All tracks carry a blank ISRC tag; the off-Qobuz bonus must still be
        # flagged so the upgrade wipe-replace can't silently delete it.
        qobuz = [_qt("T1", isrc=" "), _qt("T2", isrc=" ")]
        existing = [_et("Bonus", isrc=" "), _et("T1", isrc=" "), _et("T2", isrc=" ")]
        extras = find_extras_in_existing(qobuz, existing)
        assert [t["title"] for t in extras] == ["Bonus"]


class TestAlbumYear:
    def test_prefers_release_date_original(self):
        album = {"release_date_original": "2021-06-15", "released_at": 0}
        assert album_year(album) == "2021"

    def test_released_at_late_night_utc_is_correct_year(self):
        # 11 PM UTC on Dec 31 2019 — local TZ could flip this to 2020
        ts = int(datetime(2019, 12, 31, 23, 0, 0, tzinfo=timezone.utc).timestamp())
        assert album_year({"released_at": ts}) == "2019"


class TestAlbumQualityLabel:
    def test_hires(self):
        album = {"maximum_bit_depth": 24, "maximum_sampling_rate": 96}
        assert "hi-res" in album_quality_label(album) and "24-bit" in album_quality_label(album)

    def test_lossy(self):
        assert album_quality_label({"maximum_bit_depth": 0, "maximum_sampling_rate": 0}) == "lossy"


class TestIsLosslessAlbum:
    def test_lossless(self):
        assert is_lossless_album({"maximum_bit_depth": 16}) is True

    def test_lossy(self):
        assert is_lossless_album({"maximum_bit_depth": 0}) is False


class TestDedupAlbums:
    def _album(self, title, year, bd=16, sr=44.1, tc=10):
        return {
            "title": title, "release_date_original": str(year),
            "maximum_bit_depth": bd, "maximum_sampling_rate": sr,
            "tracks_count": tc,
        }

    def test_deduplicates_edition_variants(self):
        albums = [self._album("Revolver", 2009), self._album("Revolver (2009 Remaster)", 2009)]
        assert len(dedup_albums(albums)) == 1

    def test_prefer_hires_picks_highest_quality(self):
        albums = [self._album("Album", 2020, bd=16, sr=44.1), self._album("Album", 2020, bd=24, sr=96)]
        result = dedup_albums(albums, prefer_hires=True)
        assert len(result) == 1 and result[0][0]["maximum_bit_depth"] == 24


class TestFilterShortReleases:
    def test_drops_short(self):
        pairs = [
            ({"title": "EP", "tracks_count": 2}, 1),
            ({"title": "Album", "tracks_count": 10}, 1),
        ]
        result = filter_short_releases(pairs, min_tracks=4)
        assert len(result) == 1 and result[0][0]["title"] == "Album"


class TestFilterSeenAlbumIds:
    def test_drops_seen_ids(self):
        pairs = [({"id": "111", "title": "A"}, 1), ({"id": "222", "title": "B"}, 1)]
        result = filter_seen_album_ids(pairs, {"111"})
        assert len(result) == 1 and result[0][0]["title"] == "B"


class TestAlbumYearInt:
    def test_valid_year(self):
        assert album_year_int({"release_date_original": "1969-01-01"}) == 1969

    def test_missing_year_returns_fallback(self):
        assert album_year_int({}) == 99999
        assert album_year_int({}, fallback=0) == 0


class TestFilterOwnedAlbums:
    def _pair(self, title, year=2020, **kw):
        return ({"title": title, "release_date_original": str(year), **kw}, 1)

    def test_exact_match_same_year_dropped(self):
        pairs = [self._pair("Revolver", year=1966), self._pair("Abbey Road")]
        owned = {"revolver": [1966]}
        result = filter_owned_albums(pairs, owned)
        assert "Revolver" not in [a["title"] for a, _ in result]

    def test_year_far_away_keeps_album(self):
        pairs = [self._pair("Revolver", year=2022)]
        assert len(filter_owned_albums(pairs, {"revolver": [1966]})) == 1

    def test_none_owned_year_drops_album(self):
        pairs = [self._pair("Revolver", year=1966)]
        assert len(filter_owned_albums(pairs, {"revolver": [None]})) == 0


class TestFilterCompilationAlbums:
    def _album(self, title, artist_name, is_compilation=False):
        return ({"title": title, "artist": {"name": artist_name},
                 "is_compilation": is_compilation}, 1)

    def test_keeps_matching_artist(self):
        pairs = [self._album("Abbey Road", "The Beatles")]
        assert len(filter_compilation_albums(pairs, "The Beatles")) == 1

    def test_drops_mismatched_artist(self):
        pairs = [self._album("Now That's What I Call Music", "Various Artists")]
        assert len(filter_compilation_albums(pairs, "The Beatles")) == 0

    def test_drops_explicit_compilation_flag(self):
        pairs = [self._album("Greatest Hits", "The Beatles", is_compilation=True)]
        assert len(filter_compilation_albums(pairs, "The Beatles")) == 0


class TestDedupAlbumVersions:
    def _album(self, title, year, bd=16, sr=44.1, tc=10):
        return {
            "title": title, "release_date_original": str(year),
            "maximum_bit_depth": bd, "maximum_sampling_rate": sr,
            "tracks_count": tc,
        }

    def test_collapses_same_title_same_year(self):
        albums = [self._album("Abbey Road", 1969), self._album("Abbey Road (Remaster)", 1969)]
        assert len(dedup_album_versions(albums)) == 1

    def test_keeps_same_title_different_years(self):
        albums = [self._album("American Football", 1999), self._album("American Football", 2016)]
        assert len(dedup_album_versions(albums)) == 2


class TestHasSeparatorMatch:
    def test_comma_space_matches(self):
        assert _has_separator_match("Beatles, McCartney", "Beatles", (", ",)) is True

    def test_no_match_returns_false(self):
        assert _has_separator_match("Beatles", "Beatles", (", ",)) is False

    def test_case_insensitive_prefix(self):
        assert _has_separator_match("beatles, Stones", "Beatles", (", ",)) is True


class TestIsMultiArtistSubset:
    def test_comma_sep(self):
        assert _is_multi_artist_subset("Beatles, McCartney", "Beatles") is True

    def test_plain_artist_is_not_subset(self):
        assert _is_multi_artist_subset("Beatles", "Beatles") is False


class TestIsSplitAlbumMerge:
    def test_year_decoration_split_is_mergeable(self, tmp_path):
        artist = tmp_path / "Bonobo"
        bare = artist / "Black Sands"
        canonical = artist / "Black Sands (2010)"
        bare.mkdir(parents=True)
        canonical.mkdir(parents=True)
        assert _is_split_album_merge(bare, canonical, "Bonobo") is True

    def test_edition_difference_is_not_merged(self, tmp_path):
        artist = tmp_path / "Bonobo"
        live = artist / "Black Sands (Live)"
        canonical = artist / "Black Sands (2010)"
        live.mkdir(parents=True)
        canonical.mkdir(parents=True)
        assert _is_split_album_merge(live, canonical, "Bonobo") is False

    def test_multi_artist_same_album_is_merged(self, tmp_path):
        collab = tmp_path / "Run DMC, Aerosmith" / "Walk This Way"
        solo = tmp_path / "Run DMC" / "Walk This Way (1986)"
        collab.mkdir(parents=True)
        solo.mkdir(parents=True)
        assert _is_split_album_merge(collab, solo, "Run DMC") is True

    def test_different_album_under_multi_artist_not_fused(self, tmp_path):
        # A solo album fuzzy-resolved into a collaboration folder must not be
        # fused with the unrelated album already there.
        collab = tmp_path / "Beats Crew, DJ Guest" / "Chapter One (2018)"
        solo = tmp_path / "Beats Crew" / "Chapter Two (2020)"
        collab.mkdir(parents=True)
        solo.mkdir(parents=True)
        assert _is_split_album_merge(collab, solo, "Beats Crew") is False

    def test_same_title_different_year_not_fused(self, tmp_path):
        artist = tmp_path / "Artist"
        a = artist / "Live (2010)"
        b = artist / "Live (2011)"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        assert _is_split_album_merge(a, b, "Artist") is False


class TestIsMigrationCandidate:
    def test_comma_space_is_candidate(self):
        assert _is_migration_candidate("Beatles, McCartney", "Beatles") is True

    def test_ampersand_is_not_candidate(self):
        assert _is_migration_candidate("Beatles & McCartney", "Beatles") is False


class TestPathsEqual:
    def test_same_file_is_equal(self, tmp_path):
        f = tmp_path / "track.flac"
        f.write_bytes(b"x")
        assert _paths_equal(f, f) is True

    def test_symlink_to_same_file_is_equal(self, tmp_path):
        real = tmp_path / "real.flac"
        real.write_bytes(b"x")
        link = tmp_path / "link.flac"
        link.symlink_to(real)
        assert _paths_equal(real, link) is True


class TestFindExpandedEdition:

    def _make_album(self, album_id, artist, title, bit_depth, sr, tracks):
        return {
            "id": album_id,
            "artist": {"name": artist},
            "title": title,
            "maximum_bit_depth": bit_depth,
            "maximum_sampling_rate": sr,
            "tracks_count": len(tracks),
            "tracks": {"items": tracks},
        }

    def _qt(self, isrc, title, disc=1):
        return {"isrc": isrc, "title": title, "media_number": disc}

    def _et(self, isrc, title, disc=1):
        return {"isrc": isrc, "title": title, "discnumber": disc}

    def test_ranking_prefers_quality_when_extras_tied(self, tmp_path):
        from types import SimpleNamespace  # noqa: PLC0415

        from qobuz_fetch.library.catalog import find_expanded_edition

        existing = [
            self._et("ISRC001", "Track 1"),
            self._et("ISRC002", "Track 2"),
        ]
        orig_tracks = [self._qt("ISRC001", "Track 1"), self._qt("ISRC002", "Track 2")]
        album = self._make_album("orig", "Test Artist", "Test Album", 16, 44.1, orig_tracks)

        cand_hires = self._make_album(
            "hires", "Test Artist", "Test Album", 24, 96.0, orig_tracks)
        cand_redbook = self._make_album(
            "redbook", "Test Artist", "Test Album", 16, 44.1, orig_tracks)

        with patch("qobuz_fetch.library.catalog.search_albums",
                   return_value=[cand_hires, cand_redbook]), \
             patch("qobuz_fetch.library.catalog.get_album",
                   side_effect=lambda aid, tok: cand_hires if aid == "hires" else cand_redbook):
            results = find_expanded_edition(album, tmp_path, existing, "tok",
                                            SimpleNamespace())

        assert len(results) == 2
        assert results[0][0]["id"] == "hires", "24-bit must rank before 16-bit"
        assert results[1][0]["id"] == "redbook"

    def test_candidate_with_fewer_extras_ranks_first(self, tmp_path):
        """A candidate that drops zero extras ranks before one that drops one."""
        from types import SimpleNamespace  # noqa: PLC0415

        from qobuz_fetch.library.catalog import find_expanded_edition

        existing = [
            self._et("ISRC001", "Track 1"),
            self._et("ISRC002", "Track 2"),
        ]
        orig_tracks = [self._qt("ISRC001", "Track 1"), self._qt("ISRC002", "Track 2")]
        album = self._make_album("orig", "Test Artist", "Test Album", 16, 44.1, orig_tracks)

        # full_A covers both existing tracks → 0 extras
        full_a_tracks = orig_tracks + [self._qt("ISRC003", "Bonus")]
        cand_a = self._make_album("cand_a", "Test Artist", "Test Album", 16, 44.1, full_a_tracks)
        # full_B only covers ISRC001 → ISRC002 becomes an extra (1 extra)
        full_b_tracks = [self._qt("ISRC001", "Track 1"), self._qt("ISRC003", "Bonus")]
        cand_b = self._make_album("cand_b", "Test Artist", "Test Album", 24, 96.0, full_b_tracks)

        with patch("qobuz_fetch.library.catalog.search_albums",
                   return_value=[cand_a, cand_b]), \
             patch("qobuz_fetch.library.catalog.get_album",
                   side_effect=lambda aid, tok: cand_a if aid == "cand_a" else cand_b):
            results = find_expanded_edition(album, tmp_path, existing, "tok",
                                            SimpleNamespace())

        # cand_a (0 extras) must rank before cand_b (1 extra), even though
        # cand_b is 24-bit.
        ids = [r[0]["id"] for r in results]
        assert ids[0] == "cand_a", "fewer extras must win regardless of quality"


class TestPredictedAlbumPathsCoversBeetsPathTemplates:
    # Users can change the beets `paths.default` template. The scanner's
    # fast-path needs to match common forms or the whole library will
    # surface as "missing" after a format change.

    def test_includes_trailing_paren_and_bracket_year(self, monkeypatch):
        from qobuz_fetch import config as cfg
        from qobuz_fetch.library.catalog import predicted_album_paths
        monkeypatch.setattr(cfg, "MUSIC_ROOT", __import__("pathlib").Path("/music"))
        monkeypatch.setattr(
            "qobuz_fetch.library.catalog._find_multi_artist_dirs",
            lambda *a, **k: [],
        )

        album = {
            "title": "Hunky Dory",
            "artist": {"name": "David Bowie"},
            "release_date_original": "1971-12-17",
        }
        paths = [str(p) for p in predicted_album_paths(album)]
        assert "/music/David Bowie/Hunky Dory (1971)" in paths
        assert "/music/David Bowie/[1971] Hunky Dory" in paths
        assert "/music/David Bowie/1971 - Hunky Dory" in paths
        assert "/music/David Bowie/Hunky Dory" in paths


class TestPrimaryArtistOf:
    # Multi-artist migration needs the FIRST artist no matter which form
    # Qobuz returns ("Jay Z, Kanye West" vs "Jay Z and Kanye West" for
    # the same album) so the migration target matches the on-disk folder.

    def test_handles_comma_space(self):
        from qobuz_fetch.library.catalog import _primary_artist_of
        assert _primary_artist_of("Jay Z, Kanye West") == "Jay Z"

    def test_handles_and(self):
        from qobuz_fetch.library.catalog import _primary_artist_of
        assert _primary_artist_of("Jay Z and Kanye West") == "Jay Z"

    def test_handles_ampersand(self):
        from qobuz_fetch.library.catalog import _primary_artist_of
        assert _primary_artist_of("Daft Punk & The Weeknd") == "Daft Punk"

    def test_handles_feat(self):
        from qobuz_fetch.library.catalog import _primary_artist_of
        assert _primary_artist_of("Bruno Mars feat. Cardi B") == "Bruno Mars"

    def test_single_artist_unchanged(self):
        from qobuz_fetch.library.catalog import _primary_artist_of
        assert _primary_artist_of("Daft Punk") == "Daft Punk"

    def test_empty_returns_empty(self):
        from qobuz_fetch.library.catalog import _primary_artist_of
        assert _primary_artist_of("") == ""


class TestMultiArtistDirLookupBridge:
    """Qobuz returns 'Jay Z and Kanye West' on some editions while the folder
    is 'Jay Z, Kanye West'. The dir lookup must bridge the two via the primary
    artist, or multi-artist migration is starved of a source dir and no-ops."""

    def test_finds_comma_folder_when_qobuz_uses_and(self, tmp_path, monkeypatch):
        from qobuz_fetch import config
        from qobuz_fetch.library.catalog import find_album_dir_filesystem
        from qobuz_fetch.library.scanner import clear_scan_caches

        monkeypatch.setattr(config, "MUSIC_ROOT", tmp_path)
        album_dir = tmp_path / "Jay Z, Kanye West" / "Watch The Throne (2011)"
        album_dir.mkdir(parents=True)
        clear_scan_caches()

        album = {
            "id": "X",
            "artist": {"name": "Jay Z and Kanye West"},
            "title": "Watch The Throne",
            "release_date_original": "2011-08-08",
        }
        found = find_album_dir_filesystem(album)
        clear_scan_caches()
        assert found is not None
        assert found.name == "Watch The Throne (2011)"
        assert found.parent.name == "Jay Z, Kanye West"
