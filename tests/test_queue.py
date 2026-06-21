"""Tests for queue/builder.py and queue/persistence.py."""
import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.persistence import (
    _deserialize_queue_item,
    _serialize_queue_item,
    clear_pending_queue,
    load_pending_queue,
    offer_resume_pending_queue,
    save_pending_queue,
)


def _qitem(title="Test", **overrides):
    defaults = dict(
        album={"id": "1", "title": title},
        album_dir=Path(f"/music/{title.lower()}"),
        label=title, missing=[], present=[],
        upgrade_only=False, auto_upgrade=False,
    )
    defaults.update(overrides)
    return _build_queue_item(**defaults)


def test_build_queue_item_defaults_and_copies_siblings():
    original = [Path("/a"), Path("/b")]
    item = _qitem(siblings_to_delete=original)
    # Runtime accounting starts clean.
    assert item["backup_path"] is None
    assert (item["n_ok"], item["n_fail"]) == (0, 0)
    assert item["imported"] is False and item["result"] is None
    # siblings_to_delete must be a copy — mutating the caller's list mustn't leak in.
    original.append(Path("/c"))
    assert len(item["siblings_to_delete"]) == 2


def test_queue_item_round_trips_and_resets_runtime_fields():
    item = _qitem(album={"id": "42", "title": "Round Trip"}, auto_upgrade=True,
                  siblings_to_delete=[Path("/music/old-edition")], quality=3)
    item["n_ok"] = 5
    item["imported"] = True
    restored = _deserialize_queue_item(_serialize_queue_item(item))
    assert restored["album_dir"] == item["album_dir"]
    assert restored["quality"] == 3
    # Runtime accounting is per-run state, not persisted — it resets on restore.
    assert restored["n_ok"] == 0 and restored["imported"] is False


def test_pending_queue_round_trips_and_clears(tmp_path, monkeypatch):
    qfile = tmp_path / "queue.json"
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
    save_pending_queue([_qitem(title="Album A")], mode="album_walk")
    items, mode, saved_at = load_pending_queue()
    assert len(items) == 1 and items[0]["album"]["title"] == "Album A"
    assert mode == "album_walk"
    datetime.fromisoformat(saved_at)        # saved_at is valid ISO
    clear_pending_queue()
    assert not qfile.exists()


def test_pending_queue_rejects_bad_payloads(tmp_path, monkeypatch):
    qfile = tmp_path / "queue.json"
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
    # A future schema version is ignored rather than mis-parsed.
    qfile.write_text(json.dumps({"version": 99, "items": [], "mode": "x", "count": 0}))
    assert load_pending_queue()[0] is None
    # A file that parses as a list/string must not crash startup.
    qfile.write_text('["a", "b"]', encoding="utf-8")
    assert load_pending_queue() == (None, None, None)


def test_pending_queue_save_failure_is_silent(tmp_path, monkeypatch):
    qfile = tmp_path / "nowrite" / "queue.json"
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
    with patch("pathlib.Path.mkdir", side_effect=OSError("no perms")):
        save_pending_queue([_qitem()], mode="album_walk")   # must not raise
    assert not qfile.exists()


def test_resume_keeps_pending_file_when_not_drained(tmp_path, monkeypatch):
    qfile = tmp_path / "queue.json"
    monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
    save_pending_queue([_qitem()], mode="walk_queue")
    monkeypatch.setattr("qobuz_librarian.queue.executor._execute_download_queue",
                        lambda items, args, token, **kw: ([], False))
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    offer_resume_pending_queue(Namespace(), "tok")
    assert qfile.exists()   # albums left to retry must survive the resume


def test_executor_gap_fill_backup_restored_when_track_returns_lossy(monkeypatch, tmp_path):
    """Queue-mode gap-fill backs up present tracks before re-ripping."""
    from qobuz_librarian.library import backup as bkmod
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "02 - kept.flac").write_bytes(b"\x00" * 1000)
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    owned = album_dir / "01 - owned.flac"
    owned.write_bytes(b"the-owned-original")
    gfb = bkmod.backup_gap_fill_files([str(owned)], album_dir)
    assert gfb is not None and not owned.exists()

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir,
        "backup_path": None,
        "gap_fill_backup_path": gfb,
        "siblings_to_delete": [],
        "n_ok": 1, "n_fail": 0, "n_lossy": 1,
        "auto_upgrade": False,
    }
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert owned.exists()
    assert owned.read_bytes() == b"the-owned-original"


def test_executor_upgrade_runs_completeness_gate_before_dropping_backup(monkeypatch, tmp_path):
    # The artist/upgrade walks bulk-upgrade through this executor, so it must run
    # the same completeness gate process.py does: a decode-clean import whose
    # rebuilt folder isn't verifiably as complete as the backup KEEPS the backup.
    from qobuz_librarian.modes import process as proc
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01.flac").write_bytes(b"new")
    backup = tmp_path / "backups" / "Album.bak"
    backup.mkdir(parents=True)
    (backup / "01.flac").write_bytes(b"old")

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: album_dir)
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir, "backup_path": backup, "gap_fill_backup_path": None,
        "siblings_to_delete": [], "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": True,
    }
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)

    monkeypatch.setattr(proc, "_upgrade_replacement_verified", lambda *a: False)
    executor._resolve_queue_item(item, args, imported_globally=True)
    assert backup.exists()                       # unverified → backup kept

    monkeypatch.setattr(proc, "_upgrade_replacement_verified", lambda *a: True)
    item["backup_path"] = backup
    executor._resolve_queue_item(item, args, imported_globally=True)
    assert not backup.exists()                   # verified → backup cleared


def test_executor_upgrade_keeps_backup_when_new_folder_not_located(monkeypatch, tmp_path):
    # Clean import but the renamed folder can't be relocated → keep the backup,
    # never restore it as a duplicate beside the fresh import.
    from qobuz_librarian.queue import executor

    album_dir = tmp_path / "music" / "Artist" / "Album"
    album_dir.mkdir(parents=True)                # original was moved aside; empty
    backup = tmp_path / "backups" / "Album.bak"
    backup.mkdir(parents=True)
    (backup / "01.flac").write_bytes(b"old")

    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)
    restored = []
    monkeypatch.setattr(executor, "restore_upgrade_backup",
                        lambda bp, dest: restored.append((bp, dest)) or True)

    item = {
        "album": {"id": "A", "artist": {"name": "Artist"}, "tracks": {"items": []}},
        "album_dir": album_dir, "backup_path": backup, "gap_fill_backup_path": None,
        "siblings_to_delete": [], "n_ok": 1, "n_fail": 0, "n_lossy": 0,
        "auto_upgrade": True,
    }
    args = Namespace(migrate_multi_artist=False, no_import=False, consolidate=False)
    executor._resolve_queue_item(item, args, imported_globally=True)

    assert backup.exists() and restored == []


def test_executor_per_album_isolation_one_album_failure_keeps_others(monkeypatch, tmp_path):
    """The whole point of the per-album pipeline: a beets failure on
    album N leaves albums 1..N-1 already imported and N+1..end still
    importable, instead of taking the whole batch down. The failing album's
    staged dir is parked under BEETS_RETRY_DIR for an import-only retry, and
    every attempted album drops out of the queue — none get re-downloaded."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    items = []
    for tag in ("A", "B", "C"):
        items.append({
            "album": {"id": tag, "title": f"Album {tag}",
                      "artist": {"name": f"Artist-{tag}"},
                      "tracks": {"items": []}},
            "album_dir": None,
            "auto_upgrade": False,
            "missing": [], "present": [], "upgrade_only": False,
            "label": tag,
            "n_ok": 1, "n_fail": 0, "n_lossy": 0,
            "failed_tracks": [], "lossy_tracks": [],
            "rate_limited": False, "elapsed": 0.0,
        })

    def fake_download(item):
        tag = item["label"]
        d = staging / f"Artist-{tag}" / f"Album {tag}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "01.flac").write_bytes(b"")

    monkeypatch.setattr(executor, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(executor, "_download_for_queue_item", fake_download)
    monkeypatch.setattr(executor, "_run_pre_import_hooks_for_dirs",
                        lambda _d, _a: [])
    # Item B fails beets ("error" = non-retryable); A and C succeed.
    by_label = {"A": "ok", "B": "error", "C": "ok"}
    seen = []

    def fake_import(album_dirs):
        # Map back to the item by its album-dir grandparent name ("Artist-X").
        artist = album_dirs[0].parent.name.split("-")[-1]
        seen.append(artist)
        return by_label[artist]

    monkeypatch.setattr(executor, "beets_import_albums", fake_import)
    monkeypatch.setattr(executor, "_consolidate_duplicate_albums", lambda: None)
    monkeypatch.setattr(executor, "_resolve_queue_item",
                        lambda item, args, imported_globally: {
                            "dir": item["album_dir"], "imported": imported_globally,
                            "result": "downloaded" if imported_globally else "failed",
                            "n_ok": item.get("n_ok", 0),
                            "n_fail": item.get("n_fail", 0),
                            "n_lossy": item.get("n_lossy", 0),
                            "auto_upgrade": False,
                        })

    args = Namespace(dry_run=False, no_import=False, no_downsample=True,
                     no_compress=True, migrate_multi_artist=False,
                     consolidate=False)
    results, drained = executor._execute_download_queue(items, args, token=None)

    assert seen == ["A", "B", "C"]
    assert [r["imported"] for r in results] == [True, False, True]
    # B's staged folder got parked; A and C's are still where the test left
    # them (beets would have moved them in real life, but we stubbed it out).
    assert not (staging / "Artist-B" / "Album B").exists()
    parked = list((staging / cfg.BEETS_RETRY_DIR).rglob("Album B"))
    assert len(parked) == 1
    # All three landed audio, so all three leave the queue: B recovers by
    # re-importing the parked copy, not by re-downloading. Nothing to resume.
    assert items == []
    assert drained is True


def test_executor_keeps_only_failed_downloads_for_retry(monkeypatch, tmp_path):
    """A flush drops every album that landed audio and keeps only the ones
    that downloaded nothing, so a resume re-downloads the genuine failures
    and never the albums already on disk."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    def make(tag, n_ok):
        return {
            "album": {"id": tag, "title": tag, "artist": {"name": tag},
                      "tracks": {"items": []}},
            "album_dir": None, "auto_upgrade": False,
            "missing": [], "present": [], "upgrade_only": False, "label": tag,
            "n_ok": n_ok, "n_fail": 0, "n_lossy": 0,
            "failed_tracks": [], "lossy_tracks": [],
            "rate_limited": False, "elapsed": 0.0,
        }

    imported = make("imported", 1)
    nothing = make("nothing", 0)
    queue = [imported, nothing]

    monkeypatch.setattr(executor, "staging_preflight", lambda _a: None)
    monkeypatch.setattr(executor, "_download_for_queue_item", lambda _i: None)
    monkeypatch.setattr(executor, "_staged_album_dirs",
                        lambda item: [staging / item["label"]] if item["n_ok"] else [])
    monkeypatch.setattr(executor, "_run_pre_import_hooks_for_dirs", lambda _d, _a: [])
    monkeypatch.setattr(executor, "beets_import_albums", lambda _d: "ok")
    monkeypatch.setattr(executor, "_consolidate_duplicate_albums", lambda: None)
    monkeypatch.setattr(executor, "_resolve_queue_item",
                        lambda item, args, ok: {
                            "dir": None, "imported": ok,
                            "result": "downloaded" if item["n_ok"] else "failed",
                            "n_ok": item["n_ok"], "n_fail": 0, "n_lossy": 0,
                            "auto_upgrade": False})

    saves = []
    args = Namespace(dry_run=False, no_import=False, no_downsample=True,
                     no_compress=True, migrate_multi_artist=False, consolidate=False)
    results, drained = executor._execute_download_queue(
        queue, args, token=None, on_progress=lambda: saves.append(list(queue)))

    assert queue == [nothing]      # imported dropped, the empty download kept
    assert drained is False
    assert len(results) == 2       # results stay 1:1 with the items passed in
    assert saves                   # progress persisted as the item dropped


def test_executor_cancel_short_circuit_labels_items_cancelled(monkeypatch, tmp_path):
    """A standing cancel short-circuits the remaining albums at the loop
    boundary; each must carry result='cancelled', not the seeded None that
    _resolve_queue_item would otherwise mislabel 'nothing_landed' (and write to
    the fetch log). Regression guard for the setdefault-on-a-present-key no-op."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    def make(tag):
        return {
            "album": {"id": tag, "title": tag, "artist": {"name": tag},
                      "tracks": {"items": []}},
            "album_dir": None, "auto_upgrade": False,
            "missing": [], "present": [], "upgrade_only": False, "label": tag,
            "n_ok": 0, "n_fail": 0, "n_lossy": 0,
            "failed_tracks": [], "lossy_tracks": [],
            "rate_limited": False, "elapsed": 0.0,
            "result": None, "snapshot_before": None,   # exactly as the builder seeds
        }

    queue = [make("A"), make("B")]
    # Cancel is already standing when the batch starts → both albums hit the
    # top-of-loop short-circuit without downloading.
    monkeypatch.setattr(executor, "is_cancel_requested", lambda: True)
    monkeypatch.setattr(executor, "staging_preflight", lambda _a: None)

    seen = []

    def fake_resolve(item, args, ok):
        # Record the label _short_circuit stamped before resolution.
        seen.append((item["label"], item.get("result")))
        return {"dir": None, "imported": ok, "result": item.get("result"),
                "n_ok": 0, "n_fail": 0, "n_lossy": 0, "auto_upgrade": False}

    monkeypatch.setattr(executor, "_resolve_queue_item", fake_resolve)

    args = Namespace(dry_run=False, no_import=False, no_downsample=True,
                     no_compress=True, migrate_multi_artist=False, consolidate=False)
    executor._execute_download_queue(queue, args, token=None)

    assert seen == [("A", "cancelled"), ("B", "cancelled")]


def test_reimport_parked_albums_clears_moved_and_keeps_skipped(monkeypatch, tmp_path):
    """A parked album is cleared only when its audio actually leaves disk on the
    retry import. A beets run that exits 0 while skipping the album (e.g. a
    library duplicate) leaves the files in place — the parked copy must be kept,
    not deleted on the strength of the exit code, since it's the only copy."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    good = staging / cfg.BEETS_RETRY_DIR / "20260101_000000-good"
    skipped = staging / cfg.BEETS_RETRY_DIR / "20260101_000001-skipped"
    (good / "Good Album").mkdir(parents=True)
    (skipped / "Dup Album").mkdir(parents=True)
    good_flac = good / "Good Album" / "01.flac"
    skipped_flac = skipped / "Dup Album" / "01.flac"
    good_flac.write_bytes(b"flac")
    skipped_flac.write_bytes(b"flac")

    def fake_import(dirs):
        # beets moves audio into the library on a real import; simulate that for
        # the good album and leave the skipped one's files where they are.
        if "good" in str(dirs[0]):
            good_flac.unlink()
        return "ok"  # exit 0 either way — the disk, not this, decides cleanup
    monkeypatch.setattr(executor, "beets_import_albums", fake_import)

    assert executor._reimport_parked_albums() is True
    assert not good.exists()           # audio moved out → parking dir cleared
    assert skipped.exists()            # files remain → kept parked, not deleted
    assert skipped_flac.exists()       # the only copy of the skipped track survives


def test_push_progress_streams_separately_and_stays_out_of_the_log():
    from qobuz_librarian.web import jobs as jm
    job = jm.Job(kind="scan")
    sub = job.subscribe()
    job.push_progress("Scanning library", 5, 10, "Beyoncé")
    line = sub.get_nowait()
    assert line.startswith(jm.PROGRESS_PREFIX)
    assert json.loads(line[len(jm.PROGRESS_PREFIX):]) == {
        "phase": "Scanning library", "current": 5, "total": 10,
        "item": "Beyoncé", "found": 0}
    # progress is a header update, not a log line
    assert job.log_lines == []
    # a late subscriber gets the current snapshot once, so a reconnect shows
    # the live header instead of a blank bar
    snap = job.subscribe().get_nowait()
    assert snap.startswith(jm.PROGRESS_PREFIX)


def test_queue_runs_post_download_truncation_recheck_on_success(monkeypatch, tmp_path):
    # The post-download length recheck must fire on the QUEUE path too — walk,
    # artist/album queue, resume, repair refill and the web single-track grab all
    # flow through _execute_download_queue, so a clean truncation in a freshly
    # filled album would otherwise never be surfaced on the bulk-fill workflow.
    from qobuz_librarian.queue import executor

    post_dir = tmp_path / "music" / "Artist" / "Album"
    post_dir.mkdir(parents=True)
    (post_dir / "01.flac").write_bytes(b"\x00" * 1000)

    rechecked = []
    monkeypatch.setattr(executor, "staging_preflight", lambda args: None)
    monkeypatch.setattr(executor, "_reimport_parked_albums", lambda: False)
    monkeypatch.setattr(executor, "snapshot_staging", lambda: set())
    monkeypatch.setattr(executor, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(executor, "_download_for_queue_item",
                        lambda item: item.update(n_ok=1, n_fail=0, n_lossy=0, elapsed=0.0))
    monkeypatch.setattr(executor, "_staged_album_dirs", lambda item: [post_dir])
    monkeypatch.setattr(executor, "_run_pre_import_hooks_for_dirs", lambda dirs, args: [])
    monkeypatch.setattr(executor, "_import_album_with_retry", lambda dirs: True)
    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: post_dir)
    monkeypatch.setattr(executor, "_count_audio_files_in", lambda _d: 1)
    monkeypatch.setattr(executor, "cleanup_duplicate_art", lambda _d: 0)
    monkeypatch.setattr(executor, "_is_split_album_merge", lambda *a: False)
    monkeypatch.setattr(executor, "_consolidate_duplicate_albums", lambda: None)
    monkeypatch.setattr(executor, "write_post_import_sidecars", lambda dirs: None)
    monkeypatch.setattr(executor, "log_fetch", lambda payload: None)
    monkeypatch.setattr(executor, "warn_if_download_truncated",
                        lambda d, token, label: rechecked.append((d, token, label)) or [])

    item = _qitem(title="Album",
                  album={"id": "A", "title": "Album", "artist": {"name": "Artist"},
                         "tracks": {"items": []}},
                  album_dir=post_dir)
    args = Namespace(dry_run=False, no_import=False, migrate_multi_artist=False,
                     consolidate=False)
    results, drained = executor._execute_download_queue([item], args, "tok")

    assert results and results[0]["result"] == "downloaded"
    assert rechecked == [(post_dir, "tok", "Album")]


def test_queue_skips_recheck_when_nothing_imported(monkeypatch, tmp_path):
    # A download that imported nothing must NOT run the recheck (there's nothing
    # fresh to verify, and the album dir may not even exist yet).
    from qobuz_librarian.queue import executor

    rechecked = []
    monkeypatch.setattr(executor, "staging_preflight", lambda args: None)
    monkeypatch.setattr(executor, "_reimport_parked_albums", lambda: False)
    monkeypatch.setattr(executor, "snapshot_staging", lambda: set())
    monkeypatch.setattr(executor, "is_cancel_requested", lambda: False)
    monkeypatch.setattr(executor, "_download_for_queue_item",
                        lambda item: item.update(n_ok=0, n_fail=1, n_lossy=0, elapsed=0.0))
    monkeypatch.setattr(executor, "find_album_dir_filesystem", lambda _a: None)
    monkeypatch.setattr(executor, "log_fetch", lambda payload: None)
    monkeypatch.setattr(executor, "warn_if_download_truncated",
                        lambda d, token, label: rechecked.append(d) or [])

    item = _qitem(title="Album",
                  album={"id": "A", "title": "Album", "artist": {"name": "Artist"},
                         "tracks": {"items": []}},
                  album_dir=tmp_path / "music" / "Artist" / "Album")
    args = Namespace(dry_run=False, no_import=False, migrate_multi_artist=False,
                     consolidate=False)
    executor._execute_download_queue([item], args, "tok")
    assert rechecked == []
