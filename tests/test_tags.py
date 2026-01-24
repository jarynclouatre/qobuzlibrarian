"""Tests for qobuz_fetch.library.tags"""
import pytest

from qobuz_fetch.library.tags import (
    beets_sanitize,
    clean_qobuz_string,
    normalize,
    similarity,
    strip_album_decorations,
    strip_edition_suffix,
)


class TestCleanQobuzString:
    def test_strips_trailing_space(self):
        assert clean_qobuz_string("Hunky Dory ") == "Hunky Dory"

    def test_strips_leading_space(self):
        assert clean_qobuz_string("  Aladdin Sane") == "Aladdin Sane"

    def test_collapses_internal_whitespace(self):
        assert clean_qobuz_string("Hunky  Dory") == "Hunky Dory"
        assert clean_qobuz_string("A\tB\nC") == "A B C"

    def test_strips_outer_double_quotes(self):
        assert clean_qobuz_string('"Heroes"') == "Heroes"

    def test_strips_outer_single_quotes(self):
        assert clean_qobuz_string("'Heroes'") == "Heroes"

    def test_preserves_internal_quotes(self):
        assert clean_qobuz_string('the "wall" album') == 'the "wall" album'

    def test_passthrough_clean_string(self):
        assert clean_qobuz_string("Blackstar") == "Blackstar"

    def test_none_returns_empty_string(self):
        assert clean_qobuz_string(None) == ""

    def test_non_string_returns_empty_string(self):
        assert clean_qobuz_string(42) == ""
        assert clean_qobuz_string([]) == ""

    def test_only_whitespace_collapses_to_empty(self):
        assert clean_qobuz_string("   ") == ""

    def test_quoted_whitespace_inside_stripped(self):
        # "Heroes " (with quotes, trailing space) — strip whitespace, then quotes.
        assert clean_qobuz_string('"Heroes" ') == "Heroes"


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

    def test_strips_bracket_year_prefix(self):
        # Beets path template `[$year] $album/` produces folders like
        # "[1971] Hunky Dory" — strip the leading year so similarity
        # against the bare Qobuz title still scores high.
        assert strip_album_decorations("[1971] Hunky Dory") == "Hunky Dory"
        assert strip_album_decorations("[2017] Album Name") == "Album Name"

    def test_strips_leading_year_dash(self):
        # `$year - $album/` produces "1971 - Hunky Dory".
        assert strip_album_decorations("1971 - Hunky Dory") == "Hunky Dory"


class TestDownsampleAliasing:
    """The user-facing toggle is "Downsample hi-res before import" — the
    underlying flag is the same value under both the legacy COMPRESS_ENABLED
    name and the canonical DOWNSAMPLE_HIRES_ENABLED. Verify both env vars
    produce the same effective config value, the cfg attributes stay in
    lockstep through settings_store, and the on-disk JSON's legacy key
    still loads.
    """

    def test_old_env_var_still_enables_flag(self, monkeypatch):
        monkeypatch.setenv("COMPRESS_ENABLED", "1")
        monkeypatch.delenv("DOWNSAMPLE_HIRES_ENABLED", raising=False)
        import importlib

        from qobuz_fetch import config as cfg
        importlib.reload(cfg)
        assert cfg.DOWNSAMPLE_HIRES_ENABLED is True
        assert cfg.COMPRESS_ENABLED is True

    def test_new_env_var_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("COMPRESS_ENABLED", "0")
        monkeypatch.setenv("DOWNSAMPLE_HIRES_ENABLED", "1")
        import importlib

        from qobuz_fetch import config as cfg
        importlib.reload(cfg)
        assert cfg.DOWNSAMPLE_HIRES_ENABLED is True
        assert cfg.COMPRESS_ENABLED is True

    def test_settings_store_apply_mirrors_legacy_key(self, monkeypatch):
        from qobuz_fetch import config as cfg
        from qobuz_fetch.web import settings_store

        monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
        monkeypatch.setattr(cfg, "COMPRESS_ENABLED", False)
        settings_store._apply({"COMPRESS_ENABLED": True})
        assert cfg.DOWNSAMPLE_HIRES_ENABLED is True
        assert cfg.COMPRESS_ENABLED is True

    def test_settings_store_apply_canonical_key_sets_both(self, monkeypatch):
        from qobuz_fetch import config as cfg
        from qobuz_fetch.web import settings_store

        monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
        monkeypatch.setattr(cfg, "COMPRESS_ENABLED", False)
        settings_store._apply({"DOWNSAMPLE_HIRES_ENABLED": True})
        assert cfg.DOWNSAMPLE_HIRES_ENABLED is True
        assert cfg.COMPRESS_ENABLED is True

    def test_have_downsample_alias_exists(self):
        from qobuz_fetch.integrations import compress as comp_mod
        assert hasattr(comp_mod, "HAVE_DOWNSAMPLE")
        assert hasattr(comp_mod, "HAVE_COMPRESS")
        assert comp_mod.HAVE_DOWNSAMPLE == comp_mod.HAVE_COMPRESS
