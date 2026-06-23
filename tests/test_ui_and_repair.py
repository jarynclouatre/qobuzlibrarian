"""Tests for the repair sweep/scanner and a few CLI entry points. The bulk of
the coverage here is the data-safety machinery around repair: truncated
originals are backed up before a re-rip, and the backup is only dropped once the
refills are proven back in place and re-verified — an outage or a still-short
re-rip must keep the backup rather than lose the only good copy.
"""
from argparse import Namespace
from unittest.mock import patch

import pytest

from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs

# ── scan_dir_for_isrc_repairs: the truncation gates ────────────────────

def _track(isrc="GB1234567890", length=240.0, path="/music/track.flac", **kw):
    return {"isrc": isrc, "length": length, "title": "Track", "path": path,
            "sample_rate": 44100, "bits": 16, "channels": 2, "tracknumber": 1, **kw}


def test_scan_isrc_repairs_truncation_gates(tmp_path):
    # Both gates (duration mismatch + decode) must fire for a "verified truncated".
    track = _track(length=169.0)
    qt = {"duration": 200.0, "title": "T", "track_number": 1}
    with patch("qobuz_librarian.repair_log.read_album_dir", return_value=[track]), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc", return_value=qt):
        assert len(scan_dir_for_isrc_repairs(tmp_path, "token")["verified_truncated"]) == 1

    # Zero Qobuz duration → no reliable comparison → don't flag healthy files.
    with patch("qobuz_librarian.repair_log.read_album_dir", return_value=[_track(length=10.0)]), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value={"duration": 0, "title": "T", "track_number": 1}):
        assert scan_dir_for_isrc_repairs(tmp_path, "token")["verified_ok"] == 1

    # No Qobuz duration BUT decode probe fails → flag corruption.
    bad = _track(length=0.0, path=str(tmp_path / "x.flac"))
    with patch("qobuz_librarian.repair_log.read_album_dir", return_value=[bad]), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value={"duration": 0, "title": "T", "track_number": 1}), \
         patch("qobuz_librarian.repair_log._flac_decode_ok", return_value=False):
        assert len(scan_dir_for_isrc_repairs(tmp_path, "token")["verified_truncated"]) == 1


# ── Repair scan: resume from an interrupted sweep ──────────────────────

def test_repair_scan_resumes_from_checkpoint(tmp_path, monkeypatch):
    """An interrupted repair sweep skips the artists already checked, restores
    the albums it flagged, and clears the checkpoint when it finishes cleanly."""
    from qobuz_librarian.library import scan_checkpoint
    from qobuz_librarian.web import flows
    monkeypatch.setattr("qobuz_librarian.config.SCAN_CHECKPOINT_FILE", tmp_path / "cp.json")

    flagged = {"kind": "repair", "title": "Old Album", "artist": "Artist A",
               "detail": "1 truncated track", "selected": True,
               "payload": {"album_dir": str(tmp_path / "Artist A" / "Old Album"),
                           "artist_name": "Artist A",
                           "verified_truncated": [{"path": "x.flac"}]}}
    scan_checkpoint.save("repair", {"Artist A"}, [flagged], {})

    (tmp_path / "Artist A").mkdir()
    (tmp_path / "Artist B" / "New Album").mkdir(parents=True)
    artists = [tmp_path / "Artist A", tmp_path / "Artist B"]

    class _Job:
        def __init__(self):
            self.candidates = []
            self.cancel_requested = False
        def add_candidate(self, **kw):
            self.candidates.append(dict(kw))
        def push_progress(self, *a, **k):
            pass
    job = _Job()

    checked = []
    def fake_scan(album_dir, token, deep=False):
        checked.append(album_dir.name)
        return {"verified_truncated": [], "verified_ok": 1, "no_isrc_tag": []}

    with patch.object(flows, "list_library_artists", return_value=artists), \
         patch.object(flows, "list_artist_album_dirs",
                      side_effect=lambda d: [p for p in d.iterdir() if p.is_dir()]), \
         patch.object(flows, "clear_scan_caches"), \
         patch("qobuz_librarian.repair_log.scan_dir_for_isrc_repairs", side_effect=fake_scan):
        flows.scan_repairs(job, "token")

    assert checked == ["New Album"]                                  # Artist A skipped
    assert any(c["title"] == "Old Album" for c in job.candidates)    # prior flag restored
    assert scan_checkpoint.load("repair") is None                    # cleared on clean finish


def test_no_isrc_redownload_failure_restores_original_folder(tmp_path, monkeypatch):
    from qobuz_librarian.web import flows
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    backup_dir = tmp_path / "backup"
    restored = {}
    monkeypatch.setattr(flows, "get_album", lambda *a: {"id": "x"})
    monkeypatch.setattr("qobuz_librarian.library.backup.backup_album_dir", lambda d: backup_dir)
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": False, "n_ok": 0})
    monkeypatch.setattr("qobuz_librarian.library.backup.restore_upgrade_backup",
                        lambda bp, d: restored.update(bp=bp, dir=d) or True)
    res = flows._redownload_damaged_album(
        {"album_dir": str(album_dir), "album_id": "x"}, "token")
    assert res["n_ok"] == 0
    assert restored == {"bp": backup_dir, "dir": album_dir}


# ── Repair: relocate refilled tracks back to the album folder ─────────

def test_repair_relocates_refilled_track_back_to_album_dir(tmp_path, monkeypatch):
    from qobuz_librarian.modes import repair
    album_dir = tmp_path / "First Fires (2013)"
    landed_dir = tmp_path / "The North Borders (2013)"
    album_dir.mkdir()
    landed_dir.mkdir()
    refill = landed_dir / "01 - First Fires.flac"
    refill.write_bytes(b"flac-bytes")
    (landed_dir / "cover.jpg").write_bytes(b"art")

    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: ([{"path": str(refill), "isrc": "GBCFB1300101"}]
                                    if d == landed_dir and refill.exists() else []))
    monkeypatch.setattr(repair, "_sync_beets_db_after_file_move", lambda *a: None)
    moved = repair._relocate_refilled_into_album_dir(
        album_dir, landed_dir, {"GBCFB1300101"}, before_names=set(), landed_was_new=True)
    assert moved == 1
    assert (album_dir / "01 - First Fires.flac").exists() and not refill.exists()
    assert not landed_dir.exists()  # invented folder removed wholesale


def test_refills_present_in_counts_duplicate_isrcs(tmp_path, monkeypatch):
    # Two truncated originals sharing one ISRC (a .1.flac collision pair, or the
    # same recording on two discs) both go to backup. The presence gate must
    # require BOTH back before the backup is trusted as redundant — a set-based
    # check passed when only one returned, deleting the backup and losing the
    # other file.
    from collections import Counter

    from qobuz_librarian.modes import repair
    wanted = Counter({"GBCFB1300101": 2})

    # Only one file with the ISRC is back → not yet present.
    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: [{"isrc": "GBCFB1300101"}])
    assert repair._refills_present_in(tmp_path, wanted) is False

    # Both back → present.
    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: [{"isrc": "GBCFB1300101"}, {"isrc": "gbcfb1300101"}])
    assert repair._refills_present_in(tmp_path, wanted) is True


def test_refills_intact_requires_every_wanted_isrc_to_reverify(tmp_path, monkeypatch):
    # Before the truncated originals' backup is trusted as redundant, the rebuilt
    # folder is re-scanned and EVERY backed-up ISRC must positively re-verify.
    # Checking only "not flagged truncated" was unsafe: an ISRC whose re-lookup
    # transiently returned nothing lands in isrc_no_match, not verified_truncated,
    # so it would read as intact and the only good copy's backup would be deleted
    # while the refill is still short. Verified ISRCs come back in Qobuz's own
    # casing, so the gate normalizes them first.
    from qobuz_librarian.modes import repair
    wanted = {"GBCFB1300101", "USRC11700001"}

    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs": ["gbcfb1300101", "USRC1-17-00001"]})
    assert repair._refills_intact(tmp_path, wanted, "tok") is True

    # One ISRC didn't re-verify → keep the backup.
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_ok_isrcs": ["GBCFB1300101"]})
    assert repair._refills_intact(tmp_path, wanted, "tok") is False


def test_refills_intact_propagates_qobuz_outage(tmp_path, monkeypatch):
    # A token loss or Qobuz outage during re-verification must propagate, not
    # collapse to "still truncated" — an outage is not a verdict on the refill.
    from qobuz_librarian.modes import repair
    wanted = {"GBCFB1300101"}

    def raise_authlost(*a, **k):
        raise repair.AuthLost("token lost")
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs", raise_authlost)
    with pytest.raises(repair.AuthLost):
        repair._refills_intact(tmp_path, wanted, "tok")

    def raise_unavailable(*a, **k):
        raise repair.QobuzUnavailable("upstream down")
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs", raise_unavailable)
    with pytest.raises(repair.QobuzUnavailable):
        repair._refills_intact(tmp_path, wanted, "tok")


def test_refills_intact_keeps_backup_on_an_unexpected_rescan_error(tmp_path, monkeypatch):
    # Any non-outage failure of the re-scan stays conservative: return False so
    # the caller keeps the backup rather than delete originals on an error we
    # can't interpret.
    from qobuz_librarian.modes import repair
    wanted = {"GBCFB1300101"}

    def boom(*a, **k):
        raise ValueError("malformed scan result")
    monkeypatch.setattr(repair, "scan_dir_for_isrc_repairs", boom)
    assert repair._refills_intact(tmp_path, wanted, "tok") is False


def test_repair_leaves_a_preexisting_track_sharing_the_recording_alone(tmp_path, monkeypatch):
    # A track that was already in the target dir's sibling album under the
    # same ISRC must NOT be moved — it isn't a refill, it's an existing copy.
    from qobuz_librarian.modes import repair
    album_dir = tmp_path / "First Fires (2013)"
    owned_dir = tmp_path / "The North Borders (2013)"
    album_dir.mkdir()
    owned_dir.mkdir()
    owned = owned_dir / "01 - First Fires.flac"
    owned.write_bytes(b"already-here")

    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: ([{"path": str(owned), "isrc": "GBCFB1300101"}]
                                    if d == owned_dir else []))
    monkeypatch.setattr(repair, "_sync_beets_db_after_file_move", lambda *a: None)
    moved = repair._relocate_refilled_into_album_dir(
        album_dir, owned_dir, {"GBCFB1300101"},
        before_names={"01 - First Fires.flac"}, landed_was_new=False)
    assert moved == 0 and owned.exists()
    assert not (album_dir / "01 - First Fires.flac").exists()


# ── CLI parse_args guards ───────────────────────────────────────────────

def _parse_argv(argv):
    import sys

    from qobuz_librarian.cli import parse_args
    with patch.object(sys, "argv", ["qobuz-librarian", *argv]):
        return parse_args()


def test_parse_args_rejects_incompatible_flag_combos():
    # Each of these combos silently dropped one side before — reject at parse.
    invalid = [
        ["--auto-safe", "Some Artist - Album"],
        ["--force", "--artist", "Radiohead"],
        ["--artist", ""],
        ["--artist", "   "],
        ["--no-catalog", "Some Artist - Album"],
        ["--include-comps", "--upgrade-walk"],
        ["--no-upgrade", "--upgrade-walk"],
        ["--include-singles", "--upgrade-walk"],
        ["--artist", "Radiohead", "--upgrade-walk"],
        ["--artist", "Four Tet", "some album"],
        ["--reset-walk-seen", "--artist", "Radiohead"],
        ["--reset-walk-seen", "Some Artist - Album"],
        ["--quiet"],
        # the local-only walk/migrate modes read none of these flags either
        ["--force", "--downsample-walk"],
        ["--include-singles", "--lyrics-walk"],
        ["--include-comps", "--migrate"],
        ["--no-catalog", "--lyrics-walk"],
    ]
    for argv in invalid:
        with pytest.raises(SystemExit):
            _parse_argv(argv)


# ── Repair: backup resolution branches (the core data-safety machinery) ─

def _call_repair_album_dir(tmp_path, monkeypatch, *, n_ok, n_fail, imported,
                           present=True, intact=True):
    import qobuz_librarian.modes.repair as repair_mod
    album_dir = tmp_path / "Artist" / "Album (2020)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - Track.flac"
    track.write_bytes(b"\x00" * 200)
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("qobuz_librarian.config.REPAIR_LOG_PATH", tmp_path / "repair.log")
    monkeypatch.setattr(repair_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Album", "tracks": {"items": []}})
    # Parent-album resolution prefers the folder match; with none, it falls back
    # to the most-common ISRC album (get_album above). Stub it so the test stays
    # off the network and focused on backup resolution.
    monkeypatch.setattr(repair_mod, "find_qobuz_album_for_dir",
                        lambda *a, **k: None)

    def fake_execute(queue, args, token):
        for qi in queue:
            qi["n_ok"] = n_ok
            qi["n_fail"] = n_fail
            qi["imported"] = imported

    monkeypatch.setattr(repair_mod, "_execute_download_queue", fake_execute)
    monkeypatch.setattr(repair_mod, "append_repair_log", lambda e: True)
    # The dummy file isn't a real FLAC, so drive the post-refill verification
    # gate directly: `present` = the refilled tracks returned to album_dir,
    # `intact` = the re-scan found them no longer truncated.
    monkeypatch.setattr(repair_mod, "_refills_present_in", lambda d, w: present)
    monkeypatch.setattr(repair_mod, "_refills_intact", lambda d, w, t: intact)

    vt = [{"path": str(track), "title": "Track 01", "isrc": "USRC11111111",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0}]
    args = Namespace(force=False, yes=True, prefer_hires=False, consolidate=False, no_upgrade=False)
    return repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok"), tmp_path


def _backup_files(tmp_path):
    root = tmp_path / "backups"
    return list(root.rglob("*")) if root.exists() else []


def test_repair_backup_dropped_only_when_refills_verify_intact(tmp_path, monkeypatch):
    # Refills back in place AND verified no longer truncated: backup consumed.
    result, p = _call_repair_album_dir(tmp_path / "ok", monkeypatch,
                                       n_ok=1, n_fail=0, imported=True,
                                       present=True, intact=True)
    assert [f for f in _backup_files(p) if f.is_file()] == []
    assert result["n_ok"] == 1

    # Re-downloaded but still truncated (a short re-rip passing the decode
    # gate): the originals' backup is KEPT, not deleted on presence alone, and
    # the repair isn't reported as a success.
    result, p = _call_repair_album_dir(tmp_path / "short", monkeypatch,
                                       n_ok=1, n_fail=0, imported=True,
                                       present=True, intact=False)
    assert [f for f in _backup_files(p) if f.is_file()]
    assert result["n_ok"] == 0

    # Silent beets failure (downloads succeeded but import didn't, so nothing
    # returned to the folder): roll back to the pre-repair originals.
    result, p = _call_repair_album_dir(tmp_path / "silent", monkeypatch,
                                       n_ok=1, n_fail=0, imported=False,
                                       present=False)
    assert [f for f in _backup_files(p) if f.is_file()] == []
    assert (p / "Artist" / "Album (2020)" / "01 - Track.flac").exists()
    assert result["imported"] is False


def test_repair_backup_kept_when_downloads_fail_and_skipped_when_backup_fails(tmp_path, monkeypatch):
    # Downloads fail → backup is preserved for manual recovery.
    _call_repair_album_dir(tmp_path / "kept", monkeypatch, n_ok=0, n_fail=1, imported=False)
    assert _backup_files(tmp_path / "kept")

    # Backup itself fails → original must NOT be queued for replacement.
    import qobuz_librarian.modes.repair as repair_mod
    album_dir = tmp_path / "nb" / "Artist" / "Album (2020)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - Track.flac"
    track.write_bytes(b"\x00" * 200)
    monkeypatch.setattr(repair_mod, "find_qobuz_album_for_dir",
                        lambda *a, **k: None)
    monkeypatch.setattr(repair_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Album", "tracks": {"items": []}})
    monkeypatch.setattr(repair_mod, "backup_gap_fill_files", lambda paths, d: None)
    monkeypatch.setattr(repair_mod, "_execute_download_queue",
                        lambda *a: (_ for _ in ()).throw(
                            AssertionError("must not run when backup fails")))
    vt = [{"path": str(track), "title": "Track 01",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0}]
    args = Namespace(force=False, yes=True, prefer_hires=False, consolidate=False, no_upgrade=False)
    res = repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok")
    assert track.exists() and res["n_fail"] == len(vt)


# ── Walk-seen state: crash-safe atomic write ───────────────────────────

def test_walk_seen_records_idempotently_and_survives_a_crashed_rename(tmp_path, monkeypatch):
    import qobuz_librarian.modes.walk as walk_mod
    from qobuz_librarian.modes.walk import load_walk_seen, record_walk_seen
    f = tmp_path / "walk_seen.txt"
    monkeypatch.setattr("qobuz_librarian.config.WALK_SEEN_FILE", f)
    record_walk_seen("Radiohead")
    record_walk_seen("Radiohead")  # idempotent
    assert "radiohead" in load_walk_seen()
    prior = f.read_bytes()

    # If os.replace fails the file must not be half-written.
    monkeypatch.setattr(walk_mod.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError("crashed")))
    record_walk_seen("Portishead")
    assert f.read_bytes() == prior
    assert load_walk_seen() == {"radiohead"}


# ── Scan-report-repair classifications ──────────────────────────────────

def _call_scan_report(tmp_path, monkeypatch, *, repair_result=None,
                     verified_truncated=None, yes=True, input_return="y"):
    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian.modes.repair import _scan_report_repair
    album_dir = tmp_path / "Artist" / "Album (2022)"
    album_dir.mkdir(parents=True)
    (album_dir / "01 Track.flac").write_bytes(b"\x00" * 200)
    if verified_truncated is None:
        verified_truncated = [{"path": str(album_dir / "01 Track.flac"),
                                "title": "Track 01", "isrc": "USRC12345678",
                                "track_number": 1, "file_length": 5.0,
                                "qobuz_duration": 180.0,
                                "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "A1"}}}]
    monkeypatch.setattr(repair_mod, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_truncated": verified_truncated,
                                         "verified_ok": 0, "isrc_no_match": [], "no_isrc_tag": []})
    if repair_result is not None:
        monkeypatch.setattr(repair_mod, "repair_album_dir", lambda *a, **k: repair_result)
    monkeypatch.setattr(repair_mod, "section", lambda *a: None)
    args = Namespace(force=False, yes=yes, prefer_hires=False, consolidate=False, no_upgrade=False)
    with patch("builtins.input", return_value=input_return):
        return _scan_report_repair(album_dir, "Artist", args, "tok")


def test_scan_report_classifies_repair_outcomes(tmp_path, monkeypatch):
    # Repair succeeds → "repaired".
    assert _call_scan_report(tmp_path / "ok", monkeypatch,
                             repair_result={"n_ok": 1, "n_fail": 0, "imported": True, "backup": None}) == "repaired"
    # Downloads succeeded but beets failed silently → classified as failure.
    assert _call_scan_report(tmp_path / "silent", monkeypatch,
                             repair_result={"n_ok": 1, "n_fail": 0, "imported": False, "backup": None}) == "failed"
    # Nothing truncated → "clean".
    assert _call_scan_report(tmp_path / "clean", monkeypatch, verified_truncated=[]) == "clean"
    # User declines the prompt → "skipped".
    assert _call_scan_report(tmp_path / "skip", monkeypatch,
                             yes=False, input_return="n") == "skipped"
