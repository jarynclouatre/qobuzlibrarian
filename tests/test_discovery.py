"""The shared discovery engine — one answer to "what's missing for this artist"
that both the CLI artist mode and the web scans drive.

These exercise the engine against a real temp library on disk (so folder
resolution, edition matching and track comparison all run for real) with only
the Qobuz API stubbed. They pin the reconciled behaviour the two interfaces
must now agree on; the per-interface tests elsewhere prove each face presents
this same result.
"""
from pathlib import Path

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.library import catalog as cat
from qobuz_librarian.library import discovery
from qobuz_librarian.library.discovery import (
    DiscoveryOpts,
    find_missing_for_artist,
    find_new_releases_for_artist,
)
from qobuz_librarian.library.scanner import clear_scan_caches

# ── Fixture library + fake Qobuz ────────────────────────────────────────────────

def _qt(title, isrc="", disc=1):
    return {"title": title, "isrc": isrc, "media_number": disc, "track_number": 0}


def _et(title, isrc="", disc=1):
    return {"title": title, "isrc": isrc, "discnumber": disc}


def _album(album_id, title, artist, year, tracks, bd=16, sr=44.1):
    """A full Qobuz album dict (get_album shape), with a track list."""
    return {
        "id": album_id, "title": title,
        "artist": {"name": artist, "id": f"art-{artist}"},
        "release_date_original": f"{year}-01-01",
        "maximum_bit_depth": bd, "maximum_sampling_rate": sr,
        "tracks_count": len(tracks),
        "tracks": {"items": tracks},
    }


def _catalog_entry(album):
    """The lighter shape get_artist_albums returns — no track list."""
    return {k: v for k, v in album.items() if k != "tracks"}


class FakeQobuz:
    """Stands in for the Qobuz search/catalog API. Holds artist search hits,
    one catalog per artist id, and full album dicts keyed by id."""

    def __init__(self, *, artists, catalog, total=None):
        self._artists = artists                       # search_artists results
        self._full = {a["id"]: a for a in catalog}    # get_album by id
        self._catalog = [_catalog_entry(a) for a in catalog]
        self._total = total if total is not None else len(catalog)

    def search_artists(self, query, token, limit=5):
        return self._artists

    def get_artist_albums(self, artist_id, token, limit=500):
        return list(self._catalog), self._total

    def get_album(self, album_id, token):
        return self._full[album_id]

    def search_albums(self, query, token, limit=12):
        # Per-folder search fallback: the fixtures keep everything in the
        # catalog, so the fallback returns nothing.
        return []

    def install(self, monkeypatch):
        monkeypatch.setattr(discovery, "search_artists", self.search_artists)
        monkeypatch.setattr(discovery, "get_artist_albums", self.get_artist_albums)
        monkeypatch.setattr(discovery, "get_album", self.get_album)
        monkeypatch.setattr(cat, "get_album", self.get_album)
        monkeypatch.setattr(cat, "search_albums", self.search_albums)


def _library(monkeypatch, tmp_path, layout):
    """layout: {artist_folder: {album_folder: [existing_track_dict, ...]}}.

    Creates the dirs (one empty .flac per track so the audio-file count is
    honest) and stubs read_album_dir to return the assigned track dicts.
    """
    track_map = {}
    for artist, albums in layout.items():
        for album, tracks in albums.items():
            d = tmp_path / artist / album
            d.mkdir(parents=True)
            for i in range(len(tracks)):
                (d / f"{i + 1:02d}.flac").write_bytes(b"")
            track_map[str(d)] = tracks
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    monkeypatch.setattr(cat, "read_album_dir", lambda d: track_map.get(str(d), []))
    clear_scan_caches()


@pytest.fixture
def beatles_search():
    return [{"name": "The Beatles", "id": "beatles", "albums_count": 50}]


def _run(monkeypatch, tmp_path, *, layout, catalog, artists,
         query="The Beatles", artist_folder="The Beatles", total=None,
         hidden=None, want_missing=True, prefer_hires=True):
    _library(monkeypatch, tmp_path, layout)
    FakeQobuz(artists=artists, catalog=catalog, total=total).install(monkeypatch)
    artist_dir = (tmp_path / artist_folder) if artist_folder else None
    if artist_dir is not None and not artist_dir.exists():
        artist_dir = None
    result = find_missing_for_artist(
        query, token="tok",
        opts=DiscoveryOpts(prefer_hires=prefer_hires),
        artist_dir=artist_dir, hidden=hidden, want_missing=want_missing)
    clear_scan_caches()
    return result


def _titles(gaps):
    return sorted(g.qobuz_album.get("title") for g in gaps)


# ── Core classification ─────────────────────────────────────────────────────────

def test_fully_missing_album_is_a_gap_with_no_dir(monkeypatch, tmp_path, beatles_search):
    owned = _album("a1", "Revolver", "The Beatles", 1966,
                   [_qt(f"r{i}", f"ISRCR{i}") for i in range(11)])
    missing = _album("a2", "Abbey Road", "The Beatles", 1969,
                     [_qt(f"a{i}", f"ISRCA{i}") for i in range(10)])
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {"Revolver (1966)":
                                       [_et(f"r{i}", f"ISRCR{i}") for i in range(11)]}},
               catalog=[owned, missing], artists=beatles_search)

    fully = [g for g in res.gaps if g.on_disk_dir is None]
    assert _titles(fully) == ["Abbey Road"]
    # The owned, complete album is not a gap and not unmatched.
    assert "Revolver" not in _titles(res.gaps)
    assert [c["dir"].name for c in res.complete] == ["Revolver (1966)"]
    assert not res.unmatched_dirs


def test_partial_owned_album_reports_the_real_track_gap(monkeypatch, tmp_path, beatles_search):
    full = _album("a1", "Abbey Road", "The Beatles", 1969,
                  [_qt(f"t{i}", f"ISRC{i}") for i in range(10)])
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {"Abbey Road (1969)":
                                       [_et(f"t{i}", f"ISRC{i}") for i in range(6)]}},
               catalog=[full], artists=beatles_search)

    partials = [g for g in res.gaps if g.on_disk_dir is not None]
    assert len(partials) == 1
    gap = partials[0]
    assert gap.qobuz_album["title"] == "Abbey Road"
    assert gap.on_disk_dir.name == "Abbey Road (1969)"
    assert len(gap.present) == 6
    assert len(gap.missing) == 4


def test_deluxe_edition_gap_measured_against_the_owned_edition(monkeypatch, tmp_path, beatles_search):
    # The folder is an anniversary edition; the gap must be computed against the
    # edition that folder actually is (14 tracks), not the standard release —
    # the catalog-driven approach used to mismeasure this.
    deluxe = _album("dlx", "Severed Survival", "Autopsy", 1989,
                    [_qt(f"s{i}", f"ISRCS{i}") for i in range(14)])
    res = _run(monkeypatch, tmp_path,
               query="Autopsy", artist_folder="Autopsy",
               layout={"Autopsy": {"Severed Survival (20th Anniversary Edition) (2016)":
                                   [_et(f"s{i}", f"ISRCS{i}") for i in range(8)]}},
               catalog=[deluxe],
               artists=[{"name": "Autopsy", "id": "autopsy", "albums_count": 9}])

    partials = [g for g in res.gaps if g.on_disk_dir is not None]
    assert len(partials) == 1
    assert len(partials[0].missing) == 6
    assert len(partials[0].present) == 8


def test_owned_single_is_complete_not_missing_and_not_unmatched(monkeypatch, tmp_path, beatles_search):
    # A 1-track release the singles filter drops from the missing list is still
    # owned: it must read as complete, never offered as missing, never flagged
    # "no Qobuz match".
    single = _album("s1", "Free As A Bird", "The Beatles", 1995, [_qt("fab", "ISRCFAB")])
    album = _album("a1", "Abbey Road", "The Beatles", 1969,
                   [_qt(f"t{i}", f"ISRC{i}") for i in range(10)])
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {"Free As A Bird (1995)": [_et("fab", "ISRCFAB")]}},
               catalog=[single, album], artists=beatles_search)

    assert "Free As A Bird" not in _titles(res.gaps)
    assert not res.unmatched_dirs
    assert any(c["dir"].name == "Free As A Bird (1995)" for c in res.complete)
    # Abbey Road (10 tracks, ≥ min) is still surfaced as fully missing.
    assert "Abbey Road" in _titles(res.gaps)


def test_collaboration_owned_under_another_artist_is_not_re_offered(monkeypatch, tmp_path):
    # 'The Keeper' is filed under the collaboration folder; scanning the lead
    # artist must recognise it as already owned, not offer it as missing.
    keeper = _album("k1", "The Keeper", "Bonobo", 2009,
                    [_qt(f"k{i}", f"ISRCK{i}") for i in range(4)])
    res = _run(monkeypatch, tmp_path,
               query="Bonobo", artist_folder="Bonobo",
               layout={
                   "Bonobo": {"Migration (2017)": [_et(f"m{i}", f"ISRCM{i}") for i in range(12)]},
                   "Bonobo, Andreya Triana": {"The Keeper (2009)":
                                              [_et(f"k{i}", f"ISRCK{i}") for i in range(4)]},
               },
               catalog=[keeper,
                        _album("mig", "Migration", "Bonobo", 2017,
                               [_qt(f"m{i}", f"ISRCM{i}") for i in range(12)])],
               artists=[{"name": "Bonobo", "id": "bonobo", "albums_count": 7}])

    assert "The Keeper" not in _titles(res.gaps)


def test_zero_overlap_match_is_skipped_as_false_match(monkeypatch, tmp_path, beatles_search):
    # A folder that fuzz-resolves to a catalog album it shares no tracks with is
    # a false match, not a whole-album gap to download over it.
    album = _album("a1", "Help", "The Beatles", 1965,
                   [_qt(f"h{i}", f"ISRCH{i}") for i in range(10)])
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {"Help (1965)":
                                       [_et(f"x{i}", f"OTHER{i}") for i in range(10)]}},
               catalog=[album], artists=beatles_search)

    assert not res.gaps
    assert any(s["reason"] == "false_match" for s in res.skipped)


def test_unmatched_folder_is_reported_for_diagnosis(monkeypatch, tmp_path, beatles_search):
    # A folder that matches nothing on Qobuz surfaces in unmatched_dirs so the
    # CLI can list it as "no Qobuz match — investigate".
    album = _album("a1", "Abbey Road", "The Beatles", 1969,
                   [_qt(f"t{i}", f"ISRC{i}") for i in range(10)])
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {
                   "Abbey Road (1969)": [_et(f"t{i}", f"ISRC{i}") for i in range(10)],
                   "Some Bootleg Nobody Has (2003)": [_et("z", "ZZZ")],
               }},
               catalog=[album], artists=beatles_search)

    assert [d.name for d in res.unmatched_dirs] == ["Some Bootleg Nobody Has (2003)"]


def test_transient_api_error_aborts_the_scan_instead_of_burying_a_folder(
        monkeypatch, tmp_path, beatles_search):
    # A transient Qobuz failure while matching an owned folder must propagate,
    # not collapse into a "no match / nothing missing" verdict — otherwise an
    # outage silently buries albums the next scan would never re-check.
    from qobuz_librarian.api.auth import QobuzUnavailable

    full = _album("a1", "Abbey Road", "The Beatles", 1969,
                  [_qt(f"t{i}", f"ISRC{i}") for i in range(10)])

    class Flaky(FakeQobuz):
        def get_album(self, album_id, token):
            raise QobuzUnavailable("the Qobuz API timed out — try again later")

    _library(monkeypatch, tmp_path,
             {"The Beatles": {"Abbey Road (1969)":
                              [_et(f"t{i}", f"ISRC{i}") for i in range(6)]}})
    Flaky(artists=beatles_search, catalog=[full]).install(monkeypatch)

    with pytest.raises(QobuzUnavailable):
        find_missing_for_artist(
            "The Beatles", token="tok", opts=DiscoveryOpts(prefer_hires=True),
            artist_dir=tmp_path / "The Beatles")


# ── Filters & options ────────────────────────────────────────────────────────────

def test_hidden_store_filters_bulk_walk_but_not_single_artist(monkeypatch, tmp_path, beatles_search):
    from qobuz_librarian.library import hidden as hidden_mod
    album = _album("a1", "Abbey Road", "The Beatles", 1969,
                   [_qt(f"t{i}", f"ISRC{i}") for i in range(10)])
    store = {hidden_mod.SCOPE_MISSING:
             {hidden_mod.album_fingerprint("The Beatles", "Abbey Road"): {}},
             hidden_mod.SCOPE_UPGRADE: {}}

    hidden_run = _run(monkeypatch, tmp_path, layout={"The Beatles": {}},
                      catalog=[album], artists=beatles_search, hidden=store)
    assert "Abbey Road" not in _titles(hidden_run.gaps)

    open_run = _run(monkeypatch, tmp_path, layout={"The Beatles": {}},
                    catalog=[album], artists=beatles_search, hidden=None)
    assert "Abbey Road" in _titles(open_run.gaps)


def test_lossy_only_album_is_not_offered(monkeypatch, tmp_path, beatles_search):
    lossy = _album("l1", "Lossy Live", "The Beatles", 2001,
                   [_qt(f"l{i}") for i in range(8)], bd=0, sr=0)
    res = _run(monkeypatch, tmp_path, layout={"The Beatles": {}},
               catalog=[lossy], artists=beatles_search)
    assert "Lossy Live" not in _titles(res.gaps)


def test_want_missing_false_yields_only_owned_gaps(monkeypatch, tmp_path, beatles_search):
    partial = _album("a1", "Abbey Road", "The Beatles", 1969,
                     [_qt(f"t{i}", f"ISRC{i}") for i in range(10)])
    absent = _album("a2", "Revolver", "The Beatles", 1966,
                    [_qt(f"r{i}", f"ISRCR{i}") for i in range(11)])
    res = _run(monkeypatch, tmp_path,
               layout={"The Beatles": {"Abbey Road (1969)":
                                       [_et(f"t{i}", f"ISRC{i}") for i in range(6)]}},
               catalog=[partial, absent], artists=beatles_search, want_missing=False)

    assert _titles(res.gaps) == ["Abbey Road"]
    assert all(g.on_disk_dir is not None for g in res.gaps)


# ── New-release quickscan ─────────────────────────────────────────────────────

def test_new_releases_surface_only_what_appeared_since_the_baseline(
        monkeypatch, tmp_path):
    # resolve_artist hands back an int id (as Qobuz does) but the baseline is
    # persisted as JSON, so it comes back string-keyed — the engine must match
    # the two or it re-baselines forever and never surfaces anything.
    owned = _album(101, "Ocean Eyes", "Billie Eilish", 2016,
                   [_qt(f"o{i}", f"ISRCO{i}") for i in range(4)])
    old = _album(202, "Happier Than Ever", "Billie Eilish", 2021,
                 [_qt(f"h{i}", f"ISRCH{i}") for i in range(16)])
    fresh = _album(303, "Hit Me Hard And Soft", "Billie Eilish", 2024,
                   [_qt(f"s{i}", f"ISRCS{i}") for i in range(10)])
    _library(monkeypatch, tmp_path,
             {"Billie Eilish": {"Ocean Eyes (2016)":
                                [_et(f"o{i}", f"ISRCO{i}") for i in range(4)]}})
    FakeQobuz(artists=[{"name": "Billie Eilish", "id": 2867335}],
              catalog=[owned, old, fresh]).install(monkeypatch)
    monkeypatch.setattr(discovery, "_resolve_cache", {})
    ad = tmp_path / "Billie Eilish"
    opts = DiscoveryOpts(prefer_hires=True)

    first = find_new_releases_for_artist("Billie Eilish", token="tok", opts=opts,
                                         seen_by_id=None, artist_dir=ad)
    assert first.new_gaps == []                       # first check only baselines
    assert set(first.current_ids) == {"101", "202", "303"}

    # A later check that already knew the old catalog (string-keyed, as stored)
    # surfaces only the unowned album that's new since — not the owned one, not
    # the one it had already seen.
    later = find_new_releases_for_artist(
        "Billie Eilish", token="tok", opts=opts,
        seen_by_id={str(first.artist_id): ["101", "202"]}, artist_dir=ad)
    assert _titles(later.new_gaps) == ["Hit Me Hard And Soft"]

    caught_up = find_new_releases_for_artist(
        "Billie Eilish", token="tok", opts=opts,
        seen_by_id={str(first.artist_id): first.current_ids}, artist_dir=ad)
    assert caught_up.new_gaps == []


def test_resolution_uses_deepest_catalog_over_bare_name_twin(monkeypatch, tmp_path):
    # D6: the engine inherits the article-stripping, deepest-catalog resolver,
    # so 'beatles' resolves to the real 'The Beatles', not the bare-name twin.
    album = _album("a1", "Abbey Road", "The Beatles", 1969,
                   [_qt(f"t{i}", f"ISRC{i}") for i in range(10)])
    monkeypatch.setattr(discovery, "_resolve_cache", {})
    res = _run(monkeypatch, tmp_path, query="beatles", artist_folder=None,
               layout={}, catalog=[album],
               artists=[{"name": "Beatles", "id": "twin", "albums_count": 12},
                        {"name": "The Beatles", "id": "real", "albums_count": 530}])
    assert res.artist_id == "real"
    assert res.artist_name == "The Beatles"


def test_resolve_artist_uses_cache_and_skips_the_search(monkeypatch):
    # A cached artist resolves without hitting the search API — the re-scan
    # speed win. (Misses aren't cached, so they re-try each scan.)
    monkeypatch.setattr(discovery, "_resolve_cache", {"the who": [45964, "The Who"]})

    def _boom(*a, **k):
        raise AssertionError("search_artists must not run on a cache hit")
    monkeypatch.setattr(discovery, "search_artists", _boom)
    assert discovery.resolve_artist("the who", "tok") == (45964, "The Who")
