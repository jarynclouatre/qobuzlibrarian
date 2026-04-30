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


def test_load_tolerates_a_corrupt_file(monkeypatch, tmp_path):
    p = tmp_path / "h.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", p)
    assert hidden.load() == {"missing": {}, "upgrade": {}, "downsample": {}}


