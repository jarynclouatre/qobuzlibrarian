"""Track-counting invariants for the full-album rip reconciliation path.

The summary block must keep ``n_ok + n_lossy + n_fail`` equal to the number
of tracks attempted. A lossy fallback (file landed as MP3, deleted) belongs
in the lossy bucket only — it must not also be flagged in the failed list.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _run_process_album_full(monkeypatch, tmp_path, *,
                            kept_filenames, lossy_filenames):
    """Drive process_album down the full-album rip path with controlled
    kept/lossy snapshots. Returns the result dict + the captured local
    variables we need to assert against."""
    from qobuz_fetch import config as cfg
    from qobuz_fetch.modes import process as proc

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
    monkeypatch.setattr(proc, "_pre_import_staging_hooks", lambda _a: [])
    monkeypatch.setattr(proc, "beets_import_paths", lambda: True)
    monkeypatch.setattr(proc, "cleanup_duplicate_art", lambda _d: 0)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
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
    """A track that lands as MP3 (Qobuz hi-res fallback → deleted by
    cleanup_lossy) belongs in n_lossy only. The same title must not also
    appear in failed_tracks or be counted toward n_fail."""
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
    """Regression guard: when no track is lossy-deleted, the original
    n_fail math (missing - ok) still produces the right number."""
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
    """A 0-byte / lossy-deleted FLAC gets one single-track retry. When the
    retry produces a valid FLAC, the track lands in n_ok and rip_url is
    invoked exactly twice (one album rip + one retry) — no looping."""
    from qobuz_fetch import config as cfg
    from qobuz_fetch.modes import process as proc

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
    monkeypatch.setattr(proc, "beets_import_paths", lambda: True)
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
