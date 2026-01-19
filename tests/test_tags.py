"""Tests for qobuz_fetch.library.tags"""
import pytest

from qobuz_fetch.library.tags import (
    beets_sanitize,
    normalize,
    similarity,
    strip_album_decorations,
    strip_edition_suffix,
)


class TestNormalize:
    def test_basic_lowercase(self):
        assert normalize("Hello World") == "helloworld"

    def test_ascii_folds_accents(self):
        assert normalize("Café") == "cafe"
        assert normalize("Björk") == "bjork"

    def test_cjk_returns_empty(self):
        # Pure CJK strips to "" after ASCII encoding
        assert normalize("最好") == ""

    def test_numbers_kept(self):
        assert normalize("Track 1") == "track1"


class TestBeetsSanitize:
    def test_replaces_bad_chars(self):
        assert beets_sanitize("AC/DC") == "AC_DC"
        assert beets_sanitize("hello:world") == "hello_world"

    def test_strips_trailing_dot(self):
        assert beets_sanitize("Artist.") == "Artist"


class TestSimilarity:
    def test_identical_strings(self):
        assert similarity("Radiohead", "Radiohead") == 1.0

    def test_empty_both_returns_zero(self):
        # Two empty-normalize strings must NOT return 1.0
        assert similarity("", "") == 0.0

    def test_accent_fold_matches(self):
        assert similarity("Cafe", "Café") == 1.0


class TestStripEditionSuffix:
    def test_strips_remaster(self):
        assert strip_edition_suffix("Song (2014 Remaster)") == "Song"

    def test_strips_multiple_suffixes(self):
        assert strip_edition_suffix("Song (Remaster) (Mono)") == "Song"

    def test_preserves_acoustic(self):
        assert strip_edition_suffix("Song (Acoustic)") == "Song (Acoustic)"

    def test_preserves_live(self):
        assert strip_edition_suffix("Song (Live)") == "Song (Live)"

    def test_none_returns_none(self):
        assert strip_edition_suffix(None) is None


class TestStripAlbumDecorations:
    def test_strips_year_paren(self):
        assert strip_album_decorations("Revolver (2009 Remaster)") == "Revolver"

    def test_strips_colon_deluxe(self):
        assert strip_album_decorations("Cassadaga: Deluxe Edition") == "Cassadaga"

    def test_preserves_companion(self):
        # "Cassadaga: A Companion" is a distinct EP
        result = strip_album_decorations("Cassadaga: A Companion")
        assert result == "Cassadaga: A Companion"

    def test_plain_name_unchanged(self):
        assert strip_album_decorations("Radiohead") == "Radiohead"
