from pathlib import Path
from types import SimpleNamespace

from qobuz_librarian.modes.artist import run_artist_missing_albums


def _album(title, tracks_count, year):
    return {
        "id": title,
        "title": title,
        "tracks_count": tracks_count,
        "maximum_bit_depth": 16,
        "maximum_sampling_rate": 44.1,
        "artist": {"name": "Bonobo", "id": 99},
        "release_date_original": f"{year}-01-01",
    }


def test_fully_owned_album_is_hidden_but_partial_still_listed(monkeypatch, capsys):
    """An album whose every track is already on disk (e.g. a collaboration
    filed under another artist's folder) must not be offered for download,
    while an album that's genuinely missing tracks still appears."""
    catalog = [_album("The Keeper", 4, 2009), _album("Black Sands", 11, 2010)]

    def fake_existing(album):
        present = 4 if album["title"] == "The Keeper" else 3
        return [{}] * present, Path("/library/match")

    monkeypatch.setattr(
        "qobuz_librarian.modes.artist.find_existing_tracks", fake_existing)

    args = SimpleNamespace(prefer_hires=False, include_comps=True,
                           include_singles=True, dry_run=True, yes=False)

    run_artist_missing_albums("Bonobo", {}, args, "tok",
                              seed_artist_id=99, prefetched_catalog=catalog)

    out = capsys.readouterr().out
    assert "The Keeper" not in out
    assert "Black Sands" in out
    assert "3/11" in out
