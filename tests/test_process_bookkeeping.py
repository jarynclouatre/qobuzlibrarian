"""Track-counting invariants for the full-album rip reconciliation path.

The summary block must keep ``n_ok + n_lossy + n_fail`` equal to the number
of tracks attempted. A lossy fallback (file landed as MP3, deleted) belongs
in the lossy bucket only — it must not also be flagged in the failed list.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _run_process_album_full(monkeypatch, tmp_path, *,
                            kept_filenames, lossy_filenames, cancel=False,
                            qobuz_tracks_override=None, log_capture=None):
    """Drive process_album down the full-album rip path with controlled
    kept/lossy snapshots. Returns the result dict + the captured local
    variables we need to assert against."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    # The album has 7 tracks. One landed lossy (deleted), 5 landed as FLAC,
    # 1 missing entirely. Expected: n_ok=5, n_lossy=1, n_fail=1, total=7.
    qobuz_tracks = [
        {"id": 1, "title": "Track A", "duration": 200, "media_number": 1, "track_number": 1},
        {"id": 2, "title": "Track B", "duration": 210, "media_number": 1, "track_number": 2},
        {"id": 3, "title": "Track C", "duration": 220, "media_number": 1, "track_number": 3},
        {"id": 4, "title": "Track D", "duration": 230, "media_number": 1, "track_number": 4},
        {"id": 5, "title": "Track E", "duration": 240, "media_number": 1, "track_number": 5},
        # The lossy fallback (Qobuz served MP3 — deleted by cleanup_lossy).
        {"id": 6, "title": "Star", "duration": 250, "media_number": 1, "track_number": 6},
        # The actually-missing one (network glitch / true failure).
        {"id": 7, "title": "Outro", "duration": 100, "media_number": 1, "track_number": 7},
    ]
    if qobuz_tracks_override is not None:
        qobuz_tracks = qobuz_tracks_override
    album = {
        "id": "ALB1",
        "title": "Test Album",
        "artist": {"name": "Test Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 96.0,
        "tracks_count": len(qobuz_tracks),
        "tracks": {"items": qobuz_tracks},
    }

    staging = tmp_path / "staging"
    staging.mkdir()
    kept_paths = []
    for name in kept_filenames:
        p = staging / name
        p.write_bytes(b"\x00" * 200_000)
        kept_paths.append(p)

    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    # Tracks pass count so the post-cleanup retry pass sees an empty
    # staging delta — the test exercises the bookkeeping math, not retry.
    state = {"cleanup_calls": 0, "added_calls": 0}

    def fake_added(_s):
        state["added_calls"] += 1
        if state["added_calls"] == 1:
            return kept_paths + [staging / f"{n}.mp3" for n in lossy_filenames]
        return []

    def fake_cleanup(_files):
        state["cleanup_calls"] += 1
        if state["cleanup_calls"] == 1:
            return kept_paths, list(lossy_filenames)
        return [], []

    monkeypatch.setattr(proc, "files_added_since", fake_added)
    monkeypatch.setattr(proc, "cleanup_lossy", fake_cleanup)

    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: ([], None))
    monkeypatch.setattr(proc, "compute_missing",
                        lambda qts, _existing: (qts, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(proc, "find_extras_in_existing", lambda *_a, **_k: [])
    monkeypatch.setattr(proc, "rip_url", lambda *_a, **_k: (0, ""))
    monkeypatch.setattr(proc, "detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(proc, "detect_disk_full", lambda _o: False)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: cancel)
    monkeypatch.setattr(proc, "_pre_import_staging_hooks", lambda _a: [])
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "cleanup_duplicate_art", lambda _d: 0)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    def _capture_log(entry):
        if log_capture is not None:
            log_capture.append(entry)
    monkeypatch.setattr(proc, "log_fetch", _capture_log)
    monkeypatch.setattr(proc, "print_album_summary", lambda *_a, **_k: None)
    # auto_upgrade decision: classification is "no_existing", skips that branch.

    args = SimpleNamespace(
        force=False, yes=True, no_import=False, dry_run=False,
        verbose=False, consolidate=False, no_upgrade=False,
        no_compress=True, migrate_multi_artist=False,
        auto_upgrade=False, prefer_hires=False,
    )

    result = proc.process_album(album, args, token="tok")
    return result


def test_lossy_track_not_double_counted_in_failed_list(monkeypatch, tmp_path):
    result = _run_process_album_full(
        monkeypatch, tmp_path,
        kept_filenames=[
            "01 - Track A.flac",
            "02 - Track B.flac",
            "03 - Track C.flac",
            "04 - Track D.flac",
            "05 - Track E.flac",
        ],
        # 'Star' landed as MP3, got deleted — lossy bucket only.
        lossy_filenames=["06 - Star"],
    )

    assert result["n_ok"] == 5
    assert result["n_lossy"] == 1
    # Total Qobuz tracks = 7; ok=5, lossy=1, so exactly 1 truly failed.
    assert result["n_fail"] == 1
    # Sums to total tracks attempted, no double-counting.
    assert result["n_ok"] + result["n_lossy"] + result["n_fail"] == 7


def test_no_lossy_keeps_pre_existing_fail_count(monkeypatch, tmp_path):
    result = _run_process_album_full(
        monkeypatch, tmp_path,
        kept_filenames=[
            "01 - Track A.flac",
            "02 - Track B.flac",
            "03 - Track C.flac",
            "04 - Track D.flac",
            "05 - Track E.flac",
            "06 - Star.flac",
        ],
        lossy_filenames=[],
    )
    # 6 of 7 landed clean → 1 missing, 0 lossy.
    assert result["n_ok"] == 6
    assert result["n_lossy"] == 0
    assert result["n_fail"] == 1


def test_lossy_track_retried_once_recovers(monkeypatch, tmp_path):
    """A 0-byte / lossy-deleted FLAC gets one single-track retry."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    qobuz_tracks = [
        {"id": 1, "title": "Track A", "duration": 200, "media_number": 1, "track_number": 1},
        {"id": 6, "title": "Star", "duration": 250, "media_number": 1, "track_number": 6},
    ]
    album = {
        "id": "ALB1",
        "title": "Test Album",
        "artist": {"name": "Test Artist"},
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 96.0,
        "tracks_count": len(qobuz_tracks),
        "tracks": {"items": qobuz_tracks},
    }

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    # Initial download leaves Track A as a valid FLAC; Star landed as MP3
    # and got deleted by cleanup_lossy.
    track_a = staging / "01 - Track A.flac"
    track_a.write_bytes(b"\x00" * 200_000)
    retry_star = staging / "06 - Star.flac"

    # rip_url is called twice — once for the album, once for the per-track
    # retry. The retry side-effect writes the now-valid FLAC.
    rip_calls = []

    def fake_rip(url, *_a, **_k):
        rip_calls.append(url)
        if "track/" in url:
            # Per-track retry produced the missing FLAC.
            retry_star.write_bytes(b"\x00" * 200_000)
        return (0, "")

    monkeypatch.setattr(proc, "rip_url", fake_rip)

    # snapshot/files_added_since simulate the staging deltas:
    #   first pass: [track_a, mp3 placeholder]
    #   retry: [retry_star]
    calls = {"snap": 0, "added": 0}

    def fake_snapshot():
        calls["snap"] += 1
        if calls["snap"] == 1:
            return set()
        return {track_a}

    def fake_added(_prior):
        calls["added"] += 1
        if calls["added"] == 1:
            # Initial: A landed flac, Star landed as mp3 (deleted next).
            return [track_a, staging / "06 - Star.mp3"]
        # Retry pass: just the new FLAC.
        return [retry_star] if retry_star.exists() else []

    monkeypatch.setattr(proc, "snapshot_staging", fake_snapshot)
    monkeypatch.setattr(proc, "files_added_since", fake_added)

    cleanup_calls = {"n": 0}

    def fake_cleanup(files):
        cleanup_calls["n"] += 1
        if cleanup_calls["n"] == 1:
            # Initial: keep flac, delete the mp3.
            return [track_a], ["06 - Star"]
        # Retry: keep the new flac.
        return [retry_star], []

    monkeypatch.setattr(proc, "cleanup_lossy", fake_cleanup)

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: ([], None))
    monkeypatch.setattr(proc, "compute_missing",
                        lambda qts, _existing: (qts, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(proc, "find_extras_in_existing", lambda *_a, **_k: [])
    monkeypatch.setattr(proc, "detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(proc, "detect_disk_full", lambda _o: False)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "_pre_import_staging_hooks", lambda _a: [])
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "cleanup_duplicate_art", lambda _d: 0)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *_a, **_k: None)

    args = SimpleNamespace(
        force=False, yes=True, no_import=False, dry_run=False,
        verbose=False, consolidate=False, no_upgrade=False,
        no_compress=True, migrate_multi_artist=False,
        auto_upgrade=False, prefer_hires=False,
    )
    result = proc.process_album(album, args, token="tok")

    # Exactly two rip invocations — initial album rip + one per-track retry.
    # Three or more would indicate an infinite-loop risk.
    assert len(rip_calls) == 2
    assert rip_calls[0].startswith("https://play.qobuz.com/album/")
    assert rip_calls[1] == "https://play.qobuz.com/track/6"

    # Retry succeeded → both tracks land in n_ok, none in n_lossy or n_fail.
    assert result["n_ok"] == 2
    assert result["n_lossy"] == 0
    assert result["n_fail"] == 0


def test_sweep_staging_artwork_removes_artwork_dirs(monkeypatch, tmp_path):
    """`beet import` leaves streamrip's __artwork/ cover-image dirs behind in staging."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    staging.mkdir()
    artwork1 = staging / "Artist" / "Album (2020)" / "__artwork"
    artwork2 = staging / "Artist" / "Other Album (2021)" / "__artwork"
    artwork1.mkdir(parents=True)
    artwork2.mkdir(parents=True)
    (artwork1 / "cover-abc.jpg").write_bytes(b"\xff\xd8\xff")
    (artwork2 / "cover-xyz.jpg").write_bytes(b"\xff\xd8\xff")
    keep = staging / "Artist" / "Album (2020)" / "12 - Side B Track.flac"
    keep.write_bytes(b"fLaC")

    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    proc.sweep_staging_artwork()

    assert not artwork1.exists()
    assert not artwork2.exists()
    assert keep.exists()


def test_sweep_staging_artwork_missing_staging_dir_is_noop(monkeypatch, tmp_path):
    """A missing STAGING_DIR (cold start, broken mount) must not raise."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "does-not-exist")
    proc.sweep_staging_artwork()


def test_cancel_mid_rip_skips_beets_import(monkeypatch, tmp_path):
    result = _run_process_album_full(
        monkeypatch, tmp_path,
        kept_filenames=["01 - Track A.flac", "02 - Track B.flac"],
        lossy_filenames=[],
        cancel=True,
    )
    assert result["result"] == "cancelled"
    assert result["imported"] is False


def test_match_key_from_stem_handles_star_track_stem():
    """A lossy stem like "01. ★" must key to the same title as the Qobuz track,
    or the per-track retry can't match it back."""
    from qobuz_librarian.library.tags import normalize, strip_edition_suffix
    from qobuz_librarian.modes.process import _match_key_from_stem

    expected = normalize(strip_edition_suffix("★"))
    assert _match_key_from_stem("01. ★") == expected
    assert _match_key_from_stem("03 - Changes") == normalize(
        strip_edition_suffix("Changes"))


def test_edition_suffix_track_not_flagged_failed(monkeypatch, tmp_path):
    """A Qobuz title carrying an edition suffix (e.g."""
    tracks = [
        {"id": 1, "title": "Track A", "duration": 200, "media_number": 1, "track_number": 1},
        {"id": 2, "title": "Track B", "duration": 210, "media_number": 1, "track_number": 2},
        {"id": 3, "title": "Track C", "duration": 220, "media_number": 1, "track_number": 3},
        {"id": 4, "title": "Track D", "duration": 230, "media_number": 1, "track_number": 4},
        {"id": 5, "title": "Hungry Heart (Single Version)", "duration": 240,
         "media_number": 1, "track_number": 5},
        {"id": 6, "title": "Outro", "duration": 100, "media_number": 1, "track_number": 6},
    ]
    log_capture = []
    result = _run_process_album_full(
        monkeypatch, tmp_path,
        qobuz_tracks_override=tracks,
        kept_filenames=[
            "01 - Track A.flac",
            "02 - Track B.flac",
            "03 - Track C.flac",
            "04 - Track D.flac",
            "05 - Hungry Heart.flac",
        ],
        lossy_filenames=[],
        log_capture=log_capture,
    )
    assert result["n_fail"] == 1
    failed = log_capture[0]["failed_titles"]
    assert "Hungry Heart (Single Version)" not in failed
    assert failed == ["Outro"]


def test_gap_fill_backup_restored_when_track_returns_lossy(monkeypatch, tmp_path):
    """A full-album gap-fill backs up the already-owned tracks before ripping."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    qobuz_tracks = [
        {"id": i, "title": f"T{i}", "duration": 200, "media_number": 1, "track_number": i}
        for i in range(1, 6)
    ]
    album = {
        "id": "ALB", "title": "Album", "artist": {"name": "Artist"},
        "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
        "tracks_count": 5, "tracks": {"items": qobuz_tracks},
    }
    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    owned = album_dir / "01 - T1.flac"
    owned.write_bytes(b"the-owned-original")
    existing = [{"path": str(owned), "title": "T1", "tracknumber": 1, "discnumber": 1}]

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    # 4 of 5 missing → full-album re-rip, the path that backs up present tracks.
    missing = qobuz_tracks[1:]
    new_flacs = [staging / f"0{i} - T{i}.flac" for i in range(2, 6)]
    for f in new_flacs:
        f.write_bytes(b"\x00" * 1000)

    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: (existing, album_dir))
    monkeypatch.setattr(proc, "compute_missing", lambda _q, _e: (missing, [qobuz_tracks[0]]))
    monkeypatch.setattr(proc, "find_extras_in_existing", lambda *_a, **_k: [])
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "files_added_since", lambda _s: new_flacs + [staging / "01 - T1.mp3"])
    # The re-ripped owned track came back lossy and was deleted.
    monkeypatch.setattr(proc, "cleanup_lossy", lambda _f: (new_flacs, ["01 - T1"]))
    monkeypatch.setattr(proc, "rip_url", lambda *_a, **_k: (0, ""))
    monkeypatch.setattr(proc, "detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(proc, "detect_disk_full", lambda _o: False)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(proc, "_pre_import_staging_hooks", lambda _a: [])
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "cleanup_duplicate_art", lambda _d: 0)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "sweep_staging_artwork", lambda: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *_a, **_k: None)

    args = SimpleNamespace(
        force=False, yes=True, no_import=False, dry_run=False, verbose=False,
        consolidate=False, no_upgrade=False, no_compress=True,
        migrate_multi_artist=False, auto_upgrade=False, prefer_hires=False,
    )
    proc.process_album(album, args, token="tok")

    assert owned.exists()
    assert owned.read_bytes() == b"the-owned-original"


def test_staging_overflow_under_yes_exits_general_not_auth(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets as beets_mod
    from qobuz_librarian.integrations import rip as rip_mod
    from qobuz_librarian.ui_cli.errors import EXIT_GENERAL

    staging = tmp_path / "staging"
    staging.mkdir()
    for i in range(3):
        (staging / f"leftover{i}.flac").write_bytes(b"x")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "LEFTOVER_WARN_LIMIT", 1)
    monkeypatch.setattr(rip_mod, "cleanup_staging_residue", lambda: 0)

    with pytest.raises(SystemExit) as exc:
        beets_mod.staging_preflight(SimpleNamespace(yes=True))
    assert exc.value.code == EXIT_GENERAL
