"""process_album integration behaviour: a web cancel must skip the import, a
gap-fill that comes back lossy must restore the backed-up originals, and the
staging sweep/preflight guards hold. The download-phase bookkeeping itself lives
in test_download.py, against the shared run_album_download.
"""
from types import SimpleNamespace

import pytest


def _args(**over):
    base = dict(force=False, yes=True, no_import=False, dry_run=False,
                verbose=False, consolidate=False, no_upgrade=False,
                no_compress=True, migrate_multi_artist=False,
                auto_upgrade=False, prefer_hires=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_sweep_staging_artwork_removes_artwork_dirs(monkeypatch, tmp_path):
    """`beet import` leaves streamrip's __artwork/ cover-image dirs behind."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    artwork = staging / "Artist" / "Album (2020)" / "__artwork"
    artwork.mkdir(parents=True)
    (artwork / "cover-abc.jpg").write_bytes(b"\xff\xd8\xff")
    keep = staging / "Artist" / "Album (2020)" / "12 - Side B.flac"
    keep.write_bytes(b"fLaC")

    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    proc.sweep_staging_artwork()

    assert not artwork.exists()
    assert keep.exists()


def test_sweep_staging_artwork_missing_staging_dir_is_noop(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "does-not-exist")
    proc.sweep_staging_artwork()


def test_cancel_after_download_skips_beets_import(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import process as proc

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    monkeypatch.setattr(cfg, "AUTO_UPGRADE_ENABLED", False)

    tracks = [{"id": 1, "title": "A", "track_number": 1},
              {"id": 2, "title": "B", "track_number": 2}]
    album = {"id": "X", "title": "Alb", "artist": {"name": "Ar"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
             "tracks": {"items": tracks}}

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: ([], None))
    monkeypatch.setattr(proc, "compute_missing", lambda q, _e: (q, []))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    # The job was cancelled while the rip ran; process_album sees it after the
    # download returns and must discard rather than import.
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: True)

    def fake_download(**kw):
        kw["result"].update(n_ok=2, n_fail=0, n_lossy=0, failed_tracks=[],
                            lossy_tracks=[], elapsed=0.0,
                            gap_fill_backup_path=None)
        return kw["result"]
    monkeypatch.setattr(proc, "run_album_download", fake_download)

    beets_runs = []
    monkeypatch.setattr(proc, "beets_import_paths",
                        lambda *a, **k: beets_runs.append(1) or True)

    result = proc.process_album(album, _args(), token="tok")

    assert result["result"] == "cancelled"
    assert result["imported"] is False
    assert beets_runs == []


def test_gap_fill_backup_restored_when_track_returns_lossy(monkeypatch, tmp_path):
    """A full-album gap-fill stashes the owned tracks before re-ripping; if a
    re-ripped track comes back lossy, process_album's finally restores them."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian import download as dl
    from qobuz_librarian.modes import process as proc

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

    tracks = [{"id": i, "title": f"T{i}", "track_number": i} for i in range(1, 6)]
    album = {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
             "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0,
             "tracks": {"items": tracks}}
    # 4 of 5 missing → full-album re-rip, the path that backs up present tracks.
    missing = tracks[1:]
    new_flacs = [staging / f"0{i} - T{i}.flac" for i in range(2, 6)]
    for f in new_flacs:
        f.write_bytes(b"\x00" * 1000)

    monkeypatch.setattr(proc, "is_lossless_album", lambda _a: True)
    monkeypatch.setattr(proc, "find_existing_tracks", lambda _a: (existing, album_dir))
    monkeypatch.setattr(proc, "compute_missing", lambda _q, _e: (missing, [tracks[0]]))
    monkeypatch.setattr(proc, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(proc, "snapshot_staging", lambda: set())
    monkeypatch.setattr(proc, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(proc, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(proc, "_pre_import_staging_hooks", lambda _a: [])
    monkeypatch.setattr(proc, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr(proc, "cleanup_duplicate_art", lambda _d: 0)
    monkeypatch.setattr(proc, "write_post_import_sidecars", lambda _ds: None)
    monkeypatch.setattr(proc, "sweep_staging_artwork", lambda: None)
    monkeypatch.setattr(proc, "log_fetch", lambda _e: None)
    monkeypatch.setattr(proc, "print_album_summary", lambda *a, **k: None)

    # The download itself runs for real through run_album_download; only its
    # primitives are stubbed. The re-ripped owned track comes back lossy.
    monkeypatch.setattr(dl, "rip_url", lambda *a, **k: (0, ""))
    monkeypatch.setattr(dl, "files_added_since",
                        lambda _s: new_flacs + [staging / "01 - T1.mp3"])
    monkeypatch.setattr(dl, "cleanup_lossy", lambda _f: (new_flacs, ["01 - T1"], []))
    monkeypatch.setattr(dl, "snapshot_staging", lambda: set())
    monkeypatch.setattr(dl, "detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(dl, "detect_disk_full", lambda _o: False)
    monkeypatch.setattr(dl, "detect_rate_limited", lambda _o: False)
    monkeypatch.setattr(dl, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(dl, "find_extras_in_existing", lambda *a, **k: [])

    proc.process_album(album, _args(), token="tok")

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
