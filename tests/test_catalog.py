"""Tests for compute_missing and catalog matching/dedup helpers — the
multi-artist, diacritic, edition-strip, and ISRC-share edge cases that
actually bit us."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from qobuz_librarian.library.catalog import (
    _is_split_album_merge,
    album_year,
    compute_missing,
    dedup_album_versions,
    filter_owned_albums,
    find_album_dir_filesystem,
    find_extras_in_existing,
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


# ── find_album_dir_filesystem: real-world folder resolution edge cases ────

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
