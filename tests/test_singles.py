"""Single-track grabs: the hidden 'single' scope + the discovery gates that keep
a grabbed single from reading as a gap and from flooding scans with the artist's
catalogue."""
from pathlib import Path

import pytest

from qobuz_librarian.library import hidden
from qobuz_librarian.library.discovery import (
    DirMatch,
    DiscoveryResult,
    _collecting,
    _is_single,
    classify_owned_match,
)


@pytest.fixture
def store_file(tmp_path, monkeypatch):
    """Point the hidden store at a fresh per-test file."""
    from qobuz_librarian import config as cfg
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    return tmp_path / "hidden.json"


def _album(title, aid="1", artist="Allie X"):
    return {"id": aid, "title": title, "artist": {"name": artist}}


# ── the hidden 'single' scope ──────────────────────────────────────────────────

def test_mark_is_unmark_single_roundtrips(store_file):
    assert hidden.mark_single("Allie X", "Girl With No Face", "2024", "555") is True
    store = hidden.load()
    assert hidden.is_single("Allie X", "Girl With No Face", store) is True
    assert hidden.is_single("Allie X", "Some Other Album", store) is False
    # case/spacing-insensitive on the artist, like the rest of the store keys
    assert hidden.is_single("allie x", "Girl With No Face", store) is True

    assert hidden.unmark_single("Allie X", "Girl With No Face") is True
    assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is False
    # unmarking something not marked is a no-op, not an error
    assert hidden.unmark_single("Allie X", "Girl With No Face") is False


def test_mark_single_is_idempotent_and_keeps_timestamp(store_file):
    hidden.mark_single("X", "A", "2020", "1")
    ts1 = hidden.load()[hidden.SCOPE_SINGLE]
    first_ts = next(iter(ts1.values()))["ts"]
    hidden.mark_single("X", "A", "2020", "1")  # again
    bucket = hidden.load()[hidden.SCOPE_SINGLE]
    assert len(bucket) == 1
    assert next(iter(bucket.values()))["ts"] == first_ts
    assert next(iter(bucket.values()))["album_id"] == "1"


def test_n_singles_for_counts_per_artist(store_file):
    hidden.mark_single("Allie X", "A", "2020", "1")
    hidden.mark_single("Allie X", "B", "2021", "2")
    hidden.mark_single("Other", "C", "2022", "3")
    store = hidden.load()
    assert hidden.n_singles_for("Allie X", store) == 2
    assert hidden.n_singles_for("Other", store) == 1
    assert hidden.n_singles_for("Nobody", store) == 0


def test_single_scope_survives_other_scope_writes(store_file):
    # a single mark and a missing-hide must not clobber each other
    hidden.mark_single("X", "A", "2020", "1")
    hidden.hide(hidden.SCOPE_MISSING, [("X", "B", "2021")])
    store = hidden.load()
    assert hidden.is_single("X", "A", store) is True
    assert hidden.is_hidden(hidden.SCOPE_MISSING, "X", "B", store) is True


# ── the discovery gates ────────────────────────────────────────────────────────

def test_marked_partial_goes_to_singles_not_gaps(store_file):
    hidden.mark_single("Allie X", "Girl With No Face", "2024", "555")
    store = hidden.load()
    result = DiscoveryResult("aid", "Allie X")
    m = DirMatch(status="partial", album_dir=Path("/m/Allie X/Girl With No Face (2024)"),
                 qobuz_album=_album("Girl With No Face", aid="555"),
                 missing=[{"id": "t2"}], present=[{"id": "t1"}])
    handled, resolved = set(), set()
    classify_owned_match(result, m, None, store, "Allie X", handled, resolved)
    assert len(result.singles) == 1
    assert result.gaps == []
    # its album id is still accounted for, so the missing pass can't re-offer it
    assert "555" in handled


def test_unmarked_partial_is_still_a_gap(store_file):
    store = hidden.load()  # empty
    result = DiscoveryResult("aid", "Allie X")
    m = DirMatch(status="partial", album_dir=Path("/m/Allie X/Album (2024)"),
                 qobuz_album=_album("Album"), missing=[{"id": "t2"}], present=[{"id": "t1"}])
    classify_owned_match(result, m, None, store, "Allie X", set(), set())
    assert len(result.gaps) == 1
    assert result.singles == []


def test_collecting_requires_an_album_thats_not_a_single(store_file):
    hidden.mark_single("Allie X", "Girl With No Face", "2024", "555")
    store = hidden.load()
    one_single = [Path("/m/Allie X/Girl With No Face (2024)")]
    two_dirs = one_single + [Path("/m/Allie X/Cape God (2020)")]
    # only the single → not collecting (catalogue stays quiet)
    assert _collecting(store, "Allie X", one_single) is False
    # a real album alongside the single → collecting
    assert _collecting(store, "Allie X", two_dirs) is True
    # no store (an explicit single-artist request) → always show everything
    assert _collecting(None, "Allie X", one_single) is True


def test_is_single_helper_tolerates_none_album(store_file):
    store = hidden.load()
    assert _is_single(store, "X", None) is False
    assert _is_single(None, "X", _album("A")) is False
