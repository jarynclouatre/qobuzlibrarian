"""Tests for qobuz_librarian.library.tags — the gnarly bits."""
from qobuz_librarian.library.tags import (
    beets_sanitize,
    clean_qobuz_string,
    normalize,
    similarity,
    strip_album_decorations,
    strip_edition_suffix,
)


def test_clean_qobuz_string_trims_and_unquotes():
    # Outer quotes around an already-quoted title come back stripped, but
    # inner quotes are kept and whitespace collapses.
    assert clean_qobuz_string('"Heroes" ') == "Heroes"
    assert clean_qobuz_string("'Heroes'") == "Heroes"
    assert clean_qobuz_string('the "wall" album') == 'the "wall" album'
    assert clean_qobuz_string("Hunky  Dory") == "Hunky Dory"
    # None / non-string fed in from a Qobuz API field that was null.
    assert clean_qobuz_string(None) == ""
    assert clean_qobuz_string(42) == ""


def test_normalize_folds_accents_and_drops_cjk():
    assert normalize("Café") == "cafe"
    assert normalize("Björk") == "bjork"
    # Pure CJK normalizes to empty after ASCII fold — similarity must not
    # treat two such strings as a match (see test_similarity_empty).
    assert normalize("最好") == ""


def test_beets_sanitize_matches_beets_on_disk_names():
    assert beets_sanitize("AC/DC") == "AC_DC"
    assert beets_sanitize("hello:world") == "hello_world"
    # Leading/trailing dots turn into _ (not dropped) exactly as beets writes
    # them, so a folder like "...And Justice for All" resolves on a scan
    # instead of being reported missing and re-downloaded.
    assert beets_sanitize("...And Justice for All") == "_..And Justice for All"
    assert beets_sanitize("Artist.") == "Artist_"


def test_similarity_empty_both_does_not_score_1():
    # Two strings that normalize to "" must NOT match — otherwise CJK-only
    # titles would all collide with each other and with empty fields.
    assert similarity("", "") == 0.0
    assert similarity("最好", "とても") == 0.0
    assert similarity("Cafe", "Café") == 1.0


def test_strip_edition_suffix_preserves_distinct_versions():
    assert strip_edition_suffix("Song (2014 Remaster)") == "Song"
    assert strip_edition_suffix("Song (Remaster) (Mono)") == "Song"
    # Acoustic / Live are distinct recordings, not editions — leave them in.
    assert strip_edition_suffix("Song (Acoustic)") == "Song (Acoustic)"
    assert strip_edition_suffix("Song (Live)") == "Song (Live)"
    # an edition tag wrapping a performance one is still stripped, the
    # performance marker kept.
    assert strip_edition_suffix("Song (LP Version) (Remix)") == "Song (Remix)"
    # "with" mid-phrase is an edition descriptor, not a collaboration — the
    # whole edition tag still strips...
    assert strip_edition_suffix("Song (Single Version with Intro)") == "Song"
    # ...but a leading "(with X)" credit marks a distinct recording, kept.
    assert strip_edition_suffix("Song (with Beyoncé)") == "Song (with Beyoncé)"


def test_strip_album_decorations_handles_year_prefixed_folders():
    # Beets path templates `[$year] $album` and `$year - $album` produce
    # folder names like "[1971] Hunky Dory" or "1971 - Hunky Dory".
    assert strip_album_decorations("[1971] Hunky Dory") == "Hunky Dory"
    assert strip_album_decorations("1971 - Hunky Dory") == "Hunky Dory"
    assert strip_album_decorations("Revolver (2009 Remaster)") == "Revolver"
    assert strip_album_decorations("Cassadaga: Deluxe Edition") == "Cassadaga"
    # `Cassadaga: A Companion` is a distinct EP — not a deluxe edition tag.
    assert strip_album_decorations("Cassadaga: A Companion") == "Cassadaga: A Companion"


def test_strip_album_decorations_keeps_parenthesized_distinct_releases():
    # A live / acoustic / remix record is a different release, not a fancier
    # edition, so the bracketed marker stays attached and never collapses onto
    # the studio album (matching the colon form and strip_edition_suffix).
    assert strip_album_decorations("Wasting Light (Live)") == "Wasting Light (Live)"
    assert strip_album_decorations("MTV Unplugged (Acoustic)") == "MTV Unplugged (Acoustic)"
    # The trailing year still strips, but the marker survives the same call.
    assert strip_album_decorations("Album (Live) (2018)") == "Album (Live)"
    # A genuine edition tag in the same shape still goes.
    assert strip_album_decorations("Album (Deluxe) (2018)") == "Album"


def test_strip_album_decorations_keeps_a_bare_year_title():
    # If you don't guard the year-prefix strip, "1989" or "2112" gets eaten —
    # and owning the album "1989" would never suppress "1989 (Deluxe)" in the
    # gap scan.
    assert strip_album_decorations("1989 (Deluxe Edition)") == "1989"
    assert strip_album_decorations("2112 (2012 Remaster)") == "2112"
    assert strip_album_decorations("1984") == "1984"


def test_downsample_hires_env_var_compat(monkeypatch):
    # The user-facing toggle was renamed from COMPRESS_ENABLED to
    # DOWNSAMPLE_HIRES_ENABLED. Both env vars must keep working, and the
    # canonical name wins when both are set.
    monkeypatch.setenv("COMPRESS_ENABLED", "0")
    monkeypatch.setenv("DOWNSAMPLE_HIRES_ENABLED", "1")
    import importlib

    from qobuz_librarian import config as cfg
    importlib.reload(cfg)
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True
    assert cfg.COMPRESS_ENABLED is True

    # And the settings_store apply path keeps the legacy attribute in lockstep.
    from qobuz_librarian.web import settings_store
    monkeypatch.setattr(cfg, "DOWNSAMPLE_HIRES_ENABLED", False)
    monkeypatch.setattr(cfg, "COMPRESS_ENABLED", False)
    settings_store._apply({"COMPRESS_ENABLED": True})
    assert cfg.DOWNSAMPLE_HIRES_ENABLED is True
    assert cfg.COMPRESS_ENABLED is True
