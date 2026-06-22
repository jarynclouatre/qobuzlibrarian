"""Regression tests for the 2026-06-13 audit-backlog fixes.

One focused test per behavioural change applied while closing the verified
backlog, so the new behaviour can't silently regress.
"""
from unittest.mock import MagicMock, patch

# ── repair_log.scan_dir_for_isrc_repairs: only_isrcs limits the API sweep ──

def _isrc_track(isrc, length=240.0):
    return {"isrc": isrc, "length": length, "title": "T", "path": "/music/x.flac",
            "sample_rate": 44100, "bits": 16, "channels": 2, "tracknumber": 1}


def test_scan_dir_only_isrcs_skips_api_for_unlisted(tmp_path):
    from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs
    tracks = [_isrc_track("GBAAA0000001"), _isrc_track("GBBBB0000002"),
              _isrc_track("GBCCC0000003")]
    healthy = {"duration": 240.0, "title": "T", "track_number": 1}

    api = MagicMock(return_value=healthy)
    with patch("qobuz_librarian.repair_log.read_album_dir", return_value=tracks), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc", api):
        report = scan_dir_for_isrc_repairs(
            tmp_path, "tok", only_isrcs={"GBAAA0000001", "GBBBB0000002"})
    # The unlisted third ISRC is counted ok without burning an API call.
    assert api.call_count == 2
    assert report["verified_ok"] == 3

    # Default (no only_isrcs) still verifies every track against Qobuz.
    api_all = MagicMock(return_value=healthy)
    with patch("qobuz_librarian.repair_log.read_album_dir", return_value=tracks), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc", api_all):
        scan_dir_for_isrc_repairs(tmp_path, "tok")
    assert api_all.call_count == 3


# ── migrate.validate_paths: in_place preflights source writability ──────────

def test_validate_paths_in_place_requires_writable_source(tmp_path):
    from qobuz_librarian.library import migrate as engine
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    real_access = engine.os.access
    # Deny write on the SOURCE only — the destination stays writable so the
    # copy-mode dest-writability check passes and this isolates the source rule.
    with patch("qobuz_librarian.library.migrate.os.access",
               side_effect=lambda p, mode: False
               if (str(p) == str(src) and mode == engine.os.W_OK)
               else real_access(p, mode)):
        # Copy mode never consults source writability.
        assert engine.validate_paths(src, dest, in_place=False) is None
        # In-place mode moves files out of the source and refuses a read-only one.
        err = engine.validate_paths(src, dest, in_place=True)
    assert err is not None and "writable" in err.lower()


# ── catalog._catalog_candidates_for_dir: variant guard rejects a live edition ─

def test_catalog_candidates_excludes_live_variant(tmp_path):
    from qobuz_librarian.library import catalog
    album_dir = tmp_path / "The North Borders (2013)"
    studio = {"id": 1, "title": "The North Borders",
              "artist": {"name": "Bonobo"}, "maximum_bit_depth": 16}
    live = {"id": 2, "title": "The North Borders (Live)",
            "artist": {"name": "Bonobo"}, "maximum_bit_depth": 16}
    cands = catalog._catalog_candidates_for_dir(album_dir, [studio, live], "Bonobo")
    ids = {c.get("id") for c in cands}
    assert 1 in ids       # studio still matches its own folder
    assert 2 not in ids   # live edition is no longer pulled onto the studio dir
