from qobuz_librarian.library import hidden


def test_fingerprint_unifies_editions_and_ignores_year():
    base = hidden.album_fingerprint("Radiohead", "Kid A")
    # A remaster (different edition, different year) keys to the same album, so
    # a re-scan that resolves to the other edition can't slip past the hide.
    assert base == hidden.album_fingerprint("Radiohead", "Kid A (2009 Remaster)")
    assert base == hidden.album_fingerprint("radiohead", "KID A")
    assert base != hidden.album_fingerprint("Radiohead", "Amnesiac")
    # Nothing left to compare on → can't fingerprint, so it's never hidden.
    assert hidden.album_fingerprint("", "Kid A") is None
    assert hidden.album_fingerprint("Radiohead", "") is None


def test_hide_is_scoped_durable_and_restorable(monkeypatch, tmp_path):
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")
    assert hidden.hide(hidden.SCOPE_MISSING, [("Portishead", "Dummy", "1994")]) == 1
    # Re-hiding another edition of the same album is a no-op.
    assert hidden.hide(hidden.SCOPE_MISSING,
                       [("Portishead", "Dummy (Remaster)", None)]) == 0

    store = hidden.load()  # round-trips through disk
    assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
    # A missing-hide leaves the upgrade scope untouched.
    assert not hidden.is_hidden(hidden.SCOPE_UPGRADE, "Portishead", "Dummy", store)

    groups = hidden.hidden_by_artist(hidden.SCOPE_MISSING)
    assert len(groups) == 1
    assert groups[0]["artist"] == "Portishead"
    assert [(a["title"], a["year"]) for a in groups[0]["albums"]] == [("Dummy", "1994")]

    assert hidden.restore(hidden.SCOPE_MISSING, ["Portishead"]) == 1
    assert hidden.count(hidden.SCOPE_MISSING) == 0


def test_restore_albums_unhides_a_single_row_by_fingerprint(monkeypatch, tmp_path):
    # The per-album Restore button on the Hidden page sends the row's fingerprint
    # (the same key is_hidden looks up) so a user can pick out one album from a
    # multi-album artist without restoring all of that artist's hides.
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")
    hidden.hide(hidden.SCOPE_MISSING,
                [("Portishead", "Dummy", "1994"),
                 ("Portishead", "Third", "2008")])
    fp_dummy = hidden.album_fingerprint("Portishead", "Dummy")
    assert hidden.restore_albums(hidden.SCOPE_MISSING, [fp_dummy]) == 1

    store = hidden.load()
    assert not hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
    assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Third", store)

    # An unknown fingerprint is a no-op, not an error — a stale page race
    # (another tab already restored the same row) mustn't crash the request.
    assert hidden.restore_albums(hidden.SCOPE_MISSING, ["nobody|nothing"]) == 0
    # The fingerprint is also exposed on each per-album dict so the page can
    # render the right hidden form input without recomputing the key.
    [group] = hidden.hidden_by_artist(hidden.SCOPE_MISSING)
    assert [a["fp"] for a in group["albums"]] == [hidden.album_fingerprint("Portishead", "Third")]


def test_restore_matches_artist_case_and_accent_insensitively(monkeypatch, tmp_path):
    # A casing/spacing/accent drift between the posted value and the stored
    # artist must still restore — restore compares normalized, like the keys.
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")
    assert hidden.hide(hidden.SCOPE_MISSING, [("Sigur Rós", "Takk", "2005")]) == 1
    assert hidden.restore(hidden.SCOPE_MISSING, ["sigur ros"]) == 1
    assert hidden.count(hidden.SCOPE_MISSING) == 0


def test_load_tolerates_a_corrupt_file(monkeypatch, tmp_path):
    p = tmp_path / "h.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", p)
    assert hidden.load() == {"missing": {}, "upgrade": {}, "downsample": {}}


