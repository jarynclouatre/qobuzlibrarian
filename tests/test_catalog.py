"""Tests for compute_missing and catalog matching/dedup helpers — the
multi-artist, diacritic, edition-strip, and ISRC-share edge cases that
actually bit us."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qobuz_librarian.library.catalog import (
    _has_separator_match,
    _is_migration_candidate,
    _is_multi_artist_subset,
    _is_split_album_merge,
    _paths_equal,
    _primary_artist_of,
    album_quality_label,
    album_year,
    album_year_int,
    compute_missing,
    dedup_album_versions,
    filter_compilation_albums,
    filter_owned_albums,
    filter_short_releases,
    find_album_dir_filesystem,
    find_extras_in_existing,
    is_lossless_album,
    predicted_album_paths,
)


def _qt(title, isrc="", disc=1, **kw):
    return {"title": title, "isrc": isrc, "media_number": disc, **kw}


def _et(title, isrc="", disc=1, **kw):
    from qobuz_librarian.library.tags import normalize
    return {"title": title, "isrc": isrc, "discnumber": disc,
            "normalized": normalize(title), **kw}


def _qalbum(title, year, bd=16, sr=44.1, tc=10):
    return {"title": title, "release_date_original": str(year),
            "maximum_bit_depth": bd, "maximum_sampling_rate": sr,
            "tracks_count": tc}


# ── compute_missing: per-layer matching + the edge cases that bit us ────────

def test_compute_missing_isrc_match_beats_title_difference():
    # An ISRC match counts the track as present even with completely
    # different titles — that's the whole point of layer 1.
    qobuz = [_qt("Song A", isrc="USRC1234567")]
    existing = [_et("Completely Different Title", isrc="USRC1234567")]
    missing, present = compute_missing(qobuz, existing)
    assert len(present) == 1 and not missing


def test_compute_missing_disc_and_edition_handling():
    # A multi-disc album, tagged per disc on both sides: a title repeated across
    # discs needs a file for each — owning disc 1's copy doesn't cover disc 2's.
    qobuz = [_qt("Intro", disc=1), _qt("Theme", disc=1),
             _qt("Intro", disc=2), _qt("Theme", disc=2)]
    owned = [_et("Intro", disc=1), _et("Theme", disc=1),
             _et("Intro", disc=2), _et("Theme", disc=2)]
    assert not compute_missing(qobuz, owned)[0]
    m, _ = compute_missing(qobuz, owned[:2])  # own disc 1 only
    assert sorted(t["media_number"] for t in m) == [2, 2]
    # Edition suffixes on the Qobuz side strip cleanly and match the bare disk file.
    m, p = compute_missing([_qt("Song (2014 Remaster)")], [_et("Song")])
    assert len(p) == 1 and not m
    # But (Acoustic) / (Live) are distinct performances and must NOT match.
    m, p = compute_missing([_qt("Song")], [_et("Song (Acoustic)")])
    assert len(m) == 1 and not p


def test_flat_untagged_multidisc_album_is_not_reported_missing():
    # A 2-disc album ripped to one flat folder with no DISCNUMBER tags reads as
    # all disc 1, while Qobuz numbers the later disc 2. Disc-strict matching
    # would report the whole second disc missing on an album you fully own.
    qobuz = [_qt("One", disc=1), _qt("Two", disc=1),
             _qt("Three", disc=2), _qt("Four", disc=2)]
    existing = [_et("One"), _et("Two"), _et("Three"), _et("Four")]
    missing, present = compute_missing(qobuz, existing)
    assert not missing and len(present) == 4


def test_compute_missing_handles_repeats_without_double_counting():
    # An EP listing the same title twice — one file satisfies exactly one of them.
    m, p = compute_missing([_qt("Dayvan Cowboy"), _qt("Dayvan Cowboy")],
                           [_et("Dayvan Cowboy")])
    assert len(p) == 1 and len(m) == 1

    # 'Time' on disk must satisfy 'Time' but not also '(Bonus Track)'.
    m, p = compute_missing([_qt("Time"), _qt("Time (Bonus Track)")], [_et("Time")])
    assert [t["title"] for t in p] == ["Time"]
    assert [t["title"] for t in m] == ["Time (Bonus Track)"]


def test_compute_missing_does_not_pair_on_blank_isrc():
    # A whitespace-only ISRC tag must not pair two unrelated tracks.
    m, p = compute_missing([_qt("Track One", isrc="   ")],
                           [_et("Track Two", isrc="   ")])
    assert not p and len(m) == 1


def test_non_latin_titles_match_on_text_not_empty_normalization():
    # CJK titles fold to '' under normalize. They must match on the exact text,
    # not collapse to a shared empty key — otherwise '東京' would pair with any
    # other non-Latin track on the same disc.
    _, p = compute_missing([_qt("東京")], [_et("東京")])
    assert len(p) == 1
    m, p = compute_missing([_qt("東京")], [_et("大阪")])
    assert len(m) == 1 and not p
    # The same guard protects the upgrade path: a different-titled non-Latin
    # track on disk must be flagged as an extra, never silently wiped.
    extras = find_extras_in_existing([_qt("東京")], [_et("大阪")])
    assert [t["title"] for t in extras] == ["大阪"]


def test_non_latin_titles_with_a_shared_edition_tag_stay_distinct():
    # '東京 (Remaster)' and '大阪 (Remaster)' both ASCII-fold to just
    # 'remaster'; keying on that would let an owned 大阪 satisfy a missing 東京.
    qobuz = [_qt("東京 (Remaster)"), _qt("大阪 (Remaster)")]
    m, p = compute_missing(qobuz, [_et("大阪 (Remaster)")])
    assert [t["title"] for t in m] == ["東京 (Remaster)"]
    assert [t["title"] for t in p] == ["大阪 (Remaster)"]
    # the bare-title owned copy still matches the remaster via the stripped layer
    _, p = compute_missing([_qt("東京 (Remaster)")], [_et("東京")])
    assert len(p) == 1


# ── find_extras_in_existing: don't let bonus tracks get wiped on upgrade ─

def test_find_extras_flags_bonus_tracks_for_upgrade_safety():
    # The whole point: a same-title bonus track on disk must be flagged extra
    # so an upgrade wipe-replace can't silently delete it.
    extras = find_extras_in_existing(
        [_qt("Time")], [_et("Time"), _et("Time (Bonus Track)")])
    assert len(extras) == 1 and "Bonus" in extras[0]["title"]

    # Same protection when all tracks carry a blank ISRC tag — the off-Qobuz
    # bonus must still be flagged.
    extras = find_extras_in_existing(
        [_qt("T1", isrc=" "), _qt("T2", isrc=" ")],
        [_et("Bonus", isrc=" "), _et("T1", isrc=" "), _et("T2", isrc=" ")])
    assert [t["title"] for t in extras] == ["Bonus"]


# ── album_year + quality helpers ─────────────────────────────────────────

def test_album_year_handles_late_utc_release_correctly():
    # 11 PM UTC on Dec 31 — local TZ could flip the year. release_date_original
    # is preferred when present; released_at is interpreted as UTC.
    assert album_year({"release_date_original": "2021-06-15"}) == "2021"
    ts = int(datetime(2019, 12, 31, 23, 0, 0, tzinfo=timezone.utc).timestamp())
    assert album_year({"released_at": ts}) == "2019"


def test_album_quality_label_and_is_lossless():
    assert "hi-res" in album_quality_label({"maximum_bit_depth": 24, "maximum_sampling_rate": 96})
    assert album_quality_label({"maximum_bit_depth": 0, "maximum_sampling_rate": 0}) == "lossy"
    assert is_lossless_album({"maximum_bit_depth": 16}) is True
    assert is_lossless_album({"maximum_bit_depth": 0}) is False


def test_album_year_int_fallback():
    assert album_year_int({"release_date_original": "1969-01-01"}) == 1969
    # Missing year sorts last by default; an explicit fallback wins.
    assert album_year_int({}) == 99999
    assert album_year_int({}, fallback=0) == 0


# ── Dedup + filters ──────────────────────────────────────────────────────

def test_dedup_album_versions_collapses_editions_but_keeps_distinct_years():
    # Same title + same year + edition variant → collapse.
    pairs = [_qalbum("Abbey Road", 1969), _qalbum("Abbey Road (Remaster)", 1969)]
    assert len(dedup_album_versions(pairs)) == 1
    # Same title but different years → two distinct albums.
    pairs = [_qalbum("American Football", 1999), _qalbum("American Football", 2016)]
    assert len(dedup_album_versions(pairs)) == 2
    # Pure-CJK titles fold to '' under normalize — they must survive as distinct
    # entries (keyed on the raw text) rather than collapse into one.
    cjk = [_qalbum("東京", 2020), _qalbum("大阪", 2021)]
    assert len(dedup_album_versions(cjk)) == 2
    # prefer_hires picks the higher-resolution edition within a group.
    pair = [_qalbum("Album", 2020, bd=16, sr=44.1),
            _qalbum("Album", 2020, bd=24, sr=96)]
    result = dedup_album_versions(pair, prefer_hires=True)
    assert len(result) == 1 and result[0][0]["maximum_bit_depth"] == 24


def test_dedup_album_versions_picks_standard_over_bloated_deluxe():
    # A 60-track deluxe shouldn't win on track-count alone — both modes
    # target the standard album's track count.
    pairs = [
        _qalbum("Album", 2020, bd=16, sr=44.1, tc=12),
        _qalbum("Album (Deluxe Edition)", 2020, bd=24, sr=96, tc=60),
    ]
    assert dedup_album_versions(pairs, prefer_hires=True)[0][0]["tracks_count"] == 12
    assert dedup_album_versions(pairs, prefer_hires=False)[0][0]["tracks_count"] == 12


def test_dedup_album_versions_min_album_tracks_threshold_filters_an_ep():
    # NEW-4's documented case: a 3-track EP shares the normalized name with a
    # 15-track deluxe (a stripped-decoration collision). The EP's count
    # falls below _MIN_ALBUM_TRACKS (4), so the canonical pick ignores it
    # and the deluxe wins on tracks_count instead. A real same-titled EP at
    # 4+ tracks would still mislead here, but that needs release-type
    # metadata Qobuz doesn't expose to distinguish reliably.
    pairs = [
        _qalbum("Title", 2020, bd=24, sr=96, tc=3),
        _qalbum("Title (Deluxe Edition)", 2020, bd=24, sr=96, tc=15),
    ]
    assert dedup_album_versions(pairs, prefer_hires=True)[0][0]["tracks_count"] == 15
    assert dedup_album_versions(pairs, prefer_hires=False)[0][0]["tracks_count"] == 15


def test_filter_short_releases():
    short_and_full = [({"title": "EP", "tracks_count": 2}, 1),
                       ({"title": "Album", "tracks_count": 10}, 1)]
    assert [a["title"] for a, _ in filter_short_releases(short_and_full, min_tracks=4)] == ["Album"]


def test_filter_owned_albums_doesnt_swallow_sequels_or_distinct_years():
    # Owning 'Load' (1996) must NOT also drop 'Reload' (1997) — title similarity
    # alone is not identity. Owning 'Album' drops the deluxe edition variant.
    pairs = [({"title": "Reload", "release_date_original": "1997"}, 1),
             ({"title": "Album (Deluxe Edition)", "release_date_original": "2010"}, 1)]
    result = filter_owned_albums(pairs, {"load": [1996], "album": [2010]})
    assert [a["title"] for a, _ in result] == ["Reload"]

    # An exact same-title match counts as owned regardless of the year gap: a
    # far-off year is a remaster/reissue of the same work, so owning the 1966
    # Revolver suppresses a 2022 Revolver instead of re-offering it as a
    # duplicate. (The year window guards the fuzzy path, not the exact one.)
    pairs = [({"title": "Revolver", "release_date_original": "2022"}, 1)]
    assert filter_owned_albums(pairs, {"revolver": [1966]}) == []

    # An owned-year list with None still drops the match.
    pairs = [({"title": "Revolver", "release_date_original": "1966"}, 1)]
    assert filter_owned_albums(pairs, {"revolver": [None]}) == []

    # Owning only the live record must NOT hide the studio album behind it —
    # the prefix-fuzzy match has to recognise '(Live)' as a distinct release.
    pairs = [({"title": "Wasting Light", "release_date_original": "2011"}, 1)]
    assert [a["title"] for a, _ in
            filter_owned_albums(pairs, {"wastinglightlive": [2019]})] == ["Wasting Light"]


def test_filter_compilation_albums():
    by = lambda title, artist, comp=False: ({"title": title, "artist": {"name": artist},
                                              "is_compilation": comp}, 1)
    # Matching artist (with optional 'The' prefix tolerance) survives.
    assert len(filter_compilation_albums([by("Abbey Road", "The Beatles")], "The Beatles")) == 1
    assert len(filter_compilation_albums([by("Abbey Road", "The Beatles")], "Beatles")) == 1
    # Mismatched artist or explicit compilation flag is dropped.
    assert filter_compilation_albums([by("Now", "Various Artists")], "The Beatles") == []
    assert filter_compilation_albums([by("Hits", "The Beatles", comp=True)], "The Beatles") == []


# ── Multi-artist + separator handling ─────────────────────────────────────

def test_separator_and_subset_helpers():
    # 'Beatles, McCartney' contains 'Beatles' as the first comma-separated head.
    assert _has_separator_match("Beatles, McCartney", "Beatles", (", ",)) is True
    assert _has_separator_match("beatles, Stones", "Beatles", (", ",)) is True
    assert _has_separator_match("Beatles", "Beatles", (", ",)) is False
    # Multi-artist subset: 'Beatles, X' is, plain 'Beatles' isn't.
    assert _is_multi_artist_subset("Beatles, McCartney", "Beatles") is True
    assert _is_multi_artist_subset("Beatles", "Beatles") is False
    # A special-char artist matches its beets-sanitised folder ('AC/DC' → 'AC_DC').
    assert _is_multi_artist_subset("AC_DC, The Stones", "AC/DC") is True


def test_primary_artist_of_handles_all_separator_forms():
    # Same album can come back from Qobuz as comma / and / & / feat. — the
    # primary artist must be extractable from any form so multi-artist
    # migration targets the right folder.
    assert _primary_artist_of("Jay Z, Kanye West") == "Jay Z"
    assert _primary_artist_of("Jay Z and Kanye West") == "Jay Z"
    assert _primary_artist_of("Daft Punk & The Weeknd") == "Daft Punk"
    assert _primary_artist_of("Bruno Mars feat. Cardi B") == "Bruno Mars"
    assert _primary_artist_of("Daft Punk") == "Daft Punk"
    assert _primary_artist_of("") == ""


def test_is_migration_candidate_only_for_comma_form():
    # Comma is the canonical multi-artist form on disk; '&' isn't.
    assert _is_migration_candidate("Beatles, McCartney", "Beatles") is True
    assert _is_migration_candidate("Beatles & McCartney", "Beatles") is False


def test_paths_equal_resolves_symlinks(tmp_path):
    f = tmp_path / "track.flac"
    f.write_bytes(b"x")
    link = tmp_path / "link.flac"
    link.symlink_to(f)
    assert _paths_equal(f, link) is True
    assert _paths_equal(f, f) is True


# ── _is_split_album_merge: protect against fusing unrelated albums ─────────

def test_split_album_merge_rules(tmp_path):
    art = tmp_path / "Bonobo"
    # Year-decoration split → mergeable (same album, different folder name).
    (art / "Black Sands").mkdir(parents=True)
    (art / "Black Sands (2010)").mkdir()
    assert _is_split_album_merge(art / "Black Sands", art / "Black Sands (2010)", "Bonobo") is True

    # Edition difference (Live vs studio) → NOT mergeable.
    (art / "Black Sands (Live)").mkdir()
    assert _is_split_album_merge(art / "Black Sands (Live)", art / "Black Sands (2010)", "Bonobo") is False

    # Same title but different years → distinct albums, never merged.
    (art / "Live (2010)").mkdir()
    (art / "Live (2011)").mkdir()
    assert _is_split_album_merge(art / "Live (2010)", art / "Live (2011)", "Bonobo") is False


def test_split_album_merge_handles_multi_artist_folders(tmp_path):
    # 'Run DMC, Aerosmith / Walk This Way' and 'Run DMC / Walk This Way (1986)'
    # are the same album under different folder forms — mergeable.
    collab = tmp_path / "Run DMC, Aerosmith" / "Walk This Way"
    solo = tmp_path / "Run DMC" / "Walk This Way (1986)"
    collab.mkdir(parents=True)
    solo.mkdir(parents=True)
    assert _is_split_album_merge(collab, solo, "Run DMC") is True

    # A solo album fuzzy-resolved into a collaboration folder must NOT fuse
    # with the unrelated album already there.
    other_collab = tmp_path / "Beats Crew, DJ Guest" / "Chapter One (2018)"
    other_solo = tmp_path / "Beats Crew" / "Chapter Two (2020)"
    other_collab.mkdir(parents=True)
    other_solo.mkdir(parents=True)
    assert _is_split_album_merge(other_collab, other_solo, "Beats Crew") is False


# ── find_album_dir_filesystem: real-world folder resolution edge cases ────

def test_find_album_dir_bridges_multi_artist_separator_forms(tmp_path, monkeypatch):
    # Qobuz returns 'Jay Z and Kanye West'; on disk the folder is the
    # comma form. The lookup must bridge the two via the primary artist.
    from qobuz_librarian import config
    from qobuz_librarian.library.scanner import clear_scan_caches
    monkeypatch.setattr(config, "MUSIC_ROOT", tmp_path)
    (tmp_path / "Jay Z, Kanye West" / "Watch The Throne (2011)").mkdir(parents=True)
    clear_scan_caches()
    album = {"id": "X", "artist": {"name": "Jay Z and Kanye West"},
             "title": "Watch The Throne", "release_date_original": "2011-08-08"}
    found = find_album_dir_filesystem(album)
    clear_scan_caches()
    assert found is not None and found.parent.name == "Jay Z, Kanye West"


def test_find_album_dir_bridges_diacritics(tmp_path, monkeypatch):
    # Qobuz returns 'Motorhead' (ASCII); folder is 'Motörhead'. Lookup must
    # bridge or the album reads as missing.
    from qobuz_librarian import config
    from qobuz_librarian.library.scanner import clear_scan_caches
    monkeypatch.setattr(config, "MUSIC_ROOT", tmp_path)
    (tmp_path / "Motörhead" / "Ace of Spades (1980)").mkdir(parents=True)
    clear_scan_caches()
    album = {"id": "X", "artist": {"name": "Motorhead"},
             "title": "Ace of Spades", "release_date_original": "1980-11-08"}
    found = find_album_dir_filesystem(album)
    clear_scan_caches()
    assert found is not None and found.parent.name == "Motörhead"


def test_find_album_dir_does_not_match_a_live_release_to_the_studio_folder(tmp_path, monkeypatch):
    # 'The North Borders Tour. — Live.' shares the studio title prefix and
    # scores high on similarity — without a length gate it'd fuse live
    # tracks into the studio folder. The studio album must still resolve.
    from qobuz_librarian import config
    from qobuz_librarian.library.scanner import clear_scan_caches
    monkeypatch.setattr(config, "MUSIC_ROOT", tmp_path)
    (tmp_path / "Bonobo" / "The North Borders (2013)").mkdir(parents=True)
    clear_scan_caches()
    live = {"id": "L", "artist": {"name": "Bonobo"},
            "title": "The North Borders Tour. — Live.",
            "release_date_original": "2014-01-01"}
    assert find_album_dir_filesystem(live) is None
    clear_scan_caches()
    studio = {"id": "S", "artist": {"name": "Bonobo"},
              "title": "The North Borders", "release_date_original": "2013-01-01"}
    assert find_album_dir_filesystem(studio).name == "The North Borders (2013)"
    clear_scan_caches()


def test_find_album_dir_falls_through_to_lower_scored_folder_when_top_fails_coverage(
        tmp_path, monkeypatch):
    # A top-scoring folder that fails the length-coverage gate must not mask
    # a lower-scored folder in the same artist dir that's the real match.
    from qobuz_librarian import config
    from qobuz_librarian.library import catalog
    from qobuz_librarian.library.scanner import clear_scan_caches
    monkeypatch.setattr(config, "MUSIC_ROOT", tmp_path)
    (tmp_path / "Band" / "Wide Awakening Sessions Bonus").mkdir(parents=True)
    (tmp_path / "Band" / "Wide Awaknng").mkdir(parents=True)
    clear_scan_caches()
    monkeypatch.setattr(catalog, "similarity",
                        lambda a, b: 0.95 if "Sessions" in a else 0.80)
    album = {"id": "X", "artist": {"name": "Band"}, "title": "Wide Awakening"}
    found = find_album_dir_filesystem(album)
    clear_scan_caches()
    assert found is not None and found.name == "Wide Awaknng"


def test_predicted_album_paths_covers_common_beets_path_templates(monkeypatch):
    # The scanner fast-path enumerates the common forms — a beets paths.default
    # change that yields any of these must still be matched.
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))
    monkeypatch.setattr("qobuz_librarian.library.catalog._find_multi_artist_dirs",
                        lambda *a, **k: [])
    album = {"title": "Hunky Dory", "artist": {"name": "David Bowie"},
             "release_date_original": "1971-12-17"}
    paths = {str(p) for p in predicted_album_paths(album)}
    assert "/music/David Bowie/Hunky Dory (1971)" in paths
    assert "/music/David Bowie/[1971] Hunky Dory" in paths
    assert "/music/David Bowie/1971 - Hunky Dory" in paths
    assert "/music/David Bowie/Hunky Dory" in paths


def test_predicted_album_paths_keeps_live_distinct_from_studio_folder(monkeypatch):
    # A live release must not predict the studio album's bare folder. If it did,
    # a missing live album would resolve into the owned studio folder and get
    # skipped — worst when the two share a release year.
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))
    monkeypatch.setattr("qobuz_librarian.library.catalog._find_multi_artist_dirs",
                        lambda *a, **k: [])
    album = {"title": "Black Sands (Live)", "artist": {"name": "Bonobo"},
             "release_date_original": "2010-01-01"}
    names = {p.name for p in predicted_album_paths(album)}
    assert "Black Sands (Live) (2010)" in names
    assert "Black Sands (2010)" not in names
    assert "Black Sands" not in names


# ── find_expanded_edition: ranking is the gnarly bit ──────────────────────

def _exp_album(album_id, bd, sr, tracks):
    return {"id": album_id, "artist": {"name": "Test Artist"},
            "title": "Test Album", "maximum_bit_depth": bd,
            "maximum_sampling_rate": sr, "tracks_count": len(tracks),
            "tracks": {"items": tracks}}


def test_find_expanded_edition_prefers_quality_when_extras_tied(tmp_path):
    from qobuz_librarian.library.catalog import find_expanded_edition
    qt = lambda isrc, title: {"isrc": isrc, "title": title, "media_number": 1}
    et = lambda isrc, title: {"isrc": isrc, "title": title, "discnumber": 1}
    existing = [et("ISRC001", "Track 1"), et("ISRC002", "Track 2")]
    orig = _exp_album("orig", 16, 44.1, [qt("ISRC001", "Track 1"), qt("ISRC002", "Track 2")])
    hires = _exp_album("hires", 24, 96.0, orig["tracks"]["items"])
    redbook = _exp_album("redbook", 16, 44.1, orig["tracks"]["items"])

    with patch("qobuz_librarian.library.catalog.search_albums",
               return_value=[hires, redbook]), \
         patch("qobuz_librarian.library.catalog.get_album",
               side_effect=lambda aid, tok: hires if aid == "hires" else redbook):
        results = find_expanded_edition(orig, tmp_path, existing, "tok", SimpleNamespace())
    assert [r[0]["id"] for r in results] == ["hires", "redbook"]


def test_find_expanded_edition_fewer_extras_wins_over_quality(tmp_path):
    # A candidate that drops zero extras must rank before one that drops some,
    # even if the latter is hi-res.
    from qobuz_librarian.library.catalog import find_expanded_edition
    qt = lambda isrc, title: {"isrc": isrc, "title": title, "media_number": 1}
    et = lambda isrc, title: {"isrc": isrc, "title": title, "discnumber": 1}
    existing = [et("ISRC001", "Track 1"), et("ISRC002", "Track 2")]
    orig = _exp_album("orig", 16, 44.1, [qt("ISRC001", "Track 1"), qt("ISRC002", "Track 2")])
    cand_a = _exp_album("cand_a", 16, 44.1, orig["tracks"]["items"] + [qt("ISRC003", "Bonus")])
    cand_b = _exp_album("cand_b", 24, 96.0, [qt("ISRC001", "Track 1"), qt("ISRC003", "Bonus")])

    with patch("qobuz_librarian.library.catalog.search_albums",
               return_value=[cand_a, cand_b]), \
         patch("qobuz_librarian.library.catalog.get_album",
               side_effect=lambda aid, tok: cand_a if aid == "cand_a" else cand_b):
        results = find_expanded_edition(orig, tmp_path, existing, "tok", SimpleNamespace())
    assert results[0][0]["id"] == "cand_a"


def test_find_existing_tracks_skips_resolve_when_dir_passed(monkeypatch):
    # When the album folder is already resolved upstream, the second cached
    # subdir scan is wasteful — short-circuit it.
    from qobuz_librarian.library import catalog as cat_mod
    monkeypatch.setattr(cat_mod, "find_album_dir_filesystem",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("resolve called")))
    monkeypatch.setattr(cat_mod, "read_album_dir",
                        lambda _d: [{"isrc": "Z", "title": "x"}])
    out, ad = cat_mod.find_existing_tracks(
        {"id": "1", "title": "x"}, album_dir=Path("/music/Foo/Bar"))
    assert out == [{"isrc": "Z", "title": "x"}]
    assert ad == Path("/music/Foo/Bar")
