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


# ── force_track_by_track decouples repair from the ratio heuristic ─────

def test_executor_uses_per_track_urls_with_force_flag(monkeypatch):
    """11 of 14 missing → ratio 0.78 ≥ 0.7, which WOULD trigger the whole-album URL."""
    from qobuz_librarian.queue import executor

    tracks = [{"id": i, "title": f"T{i}"} for i in range(1, 15)]
    missing = tracks[:11]
    item = _build_queue_item(
        album={"id": "ALBUM99", "tracks": {"items": tracks}},
        album_dir=None, label="repair", missing=missing,
        present=[{} for _ in range(3)], upgrade_only=False,
        auto_upgrade=False, force_track_by_track=True)
    item["snapshot_before"] = set()

    seen = []

    def fake_rip(url, **kw):
        seen.append(url)
        return 0, ""

    monkeypatch.setattr(executor, "rip_url", fake_rip)
    monkeypatch.setattr(executor, "files_added_since", lambda _s: [])
    monkeypatch.setattr("qobuz_librarian.api.auth.detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(executor.time, "sleep", lambda _s: None)

    executor._download_for_queue_item(item)

    assert len(seen) == 11
    assert all("/track/" in u for u in seen)
    assert not any("/album/" in u for u in seen)


def test_executor_per_track_loop_stops_on_cancel_without_counting_failures(monkeypatch):
    from qobuz_librarian.queue import executor

    tracks = [{"id": i, "title": f"T{i}"} for i in range(1, 15)]
    item = _build_queue_item(
        album={"id": "A", "tracks": {"items": tracks}}, album_dir=None,
        label="repair", missing=tracks[:11], present=[{} for _ in range(3)],
        upgrade_only=False, auto_upgrade=False, force_track_by_track=True)
    item["snapshot_before"] = set()

    calls = {"n": 0}

    def fake_cancel():
        calls["n"] += 1
        return calls["n"] > 1          # False on the first (top) check, True after

    seen = []
    monkeypatch.setattr(executor, "is_cancel_requested", fake_cancel)
    monkeypatch.setattr(executor, "rip_url",
                        lambda url, **kw: (seen.append(url), (130, ""))[1])
    monkeypatch.setattr(executor, "files_added_since", lambda _s: [])
    monkeypatch.setattr("qobuz_librarian.api.auth.detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(executor.time, "sleep", lambda _s: None)

    executor._download_for_queue_item(item)

    assert len(seen) == 1              # stopped after the first track, not all 11
    assert item["n_fail"] == 0         # the cancel exit (130) isn't a failure


# ── download_full_album heuristic boundary ─────────────────────────────

@pytest.mark.parametrize("total,missing,expect_full", [
    (100, 69, False),  # 0.69 → per-track
    (100, 70, True),   # 0.70 → full-album
    (5, 3, False),     # below the max(4, …) floor → per-track
    (5, 4, True),      # hits the floor of 4 → full-album
])
def test_download_strategy_boundary(total, missing, expect_full, monkeypatch):
    from qobuz_librarian.queue import executor

    tracks = [{"id": i, "title": f"T{i}"} for i in range(total)]
    item = _build_queue_item(
        album={"id": "A", "tracks": {"items": tracks}},
        album_dir=None, label="x", missing=tracks[:missing],
        present=[{}], upgrade_only=False, auto_upgrade=False)
    item["snapshot_before"] = set()

    seen = []
    monkeypatch.setattr(executor, "rip_url",
                        lambda url, **kw: (seen.append(url), (0, ""))[1])
    monkeypatch.setattr(executor, "files_added_since", lambda _s: [])
    monkeypatch.setattr("qobuz_librarian.api.auth.detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(executor.time, "sleep", lambda _s: None)

    executor._download_for_queue_item(item)
    assert any("/album/" in u for u in seen) is expect_full


def test_executor_recovers_edition_suffix_track_that_landed(monkeypatch, tmp_path):
    from qobuz_librarian.queue import executor

    tracks = [{"id": i, "title": t} for i, t in enumerate(
        ["A", "B", "C", "D", "Hungry Heart (Single Version)"], 1)]
    item = _build_queue_item(
        album={"id": "ALB", "tracks": {"items": tracks}},
        album_dir=None, label="x", missing=[tracks[4]],
        present=[{} for _ in range(4)], upgrade_only=False, auto_upgrade=False)
    item["snapshot_before"] = set()

    landed = tmp_path / "05 - Hungry Heart.flac"
    landed.write_bytes(b"flac")

    monkeypatch.setattr(executor, "rip_url", lambda url, **kw: (1, "error"))
    monkeypatch.setattr(executor, "files_added_since", lambda _s: [landed])
    monkeypatch.setattr(executor, "cleanup_lossy", lambda files: (list(files), []))
    monkeypatch.setattr("qobuz_librarian.api.auth.detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(executor.time, "sleep", lambda _s: None)

    executor._download_for_queue_item(item)

    assert item["n_fail"] == 0
    assert item["failed_tracks"] == []


def test_executor_retries_lossy_track_once_recovers(monkeypatch, tmp_path):
    from qobuz_librarian.queue import executor

    tracks = [{"id": 1, "title": "A"}, {"id": 2, "title": "Star"}]
    item = _build_queue_item(
        album={"id": "ALB", "tracks": {"items": tracks}},
        album_dir=None, label="x", missing=[tracks[1]],
        present=[], upgrade_only=False, auto_upgrade=False)
    item["snapshot_before"] = set()

    recovered = tmp_path / "02 - Star.flac"
    rip_calls = []

    def fake_rip(url, **kw):
        rip_calls.append(url)
        if "track/2" in url:          # the per-track retry produces the FLAC
            recovered.write_bytes(b"\x00" * 200_000)
        return (0, "")

    monkeypatch.setattr(executor, "rip_url", fake_rip)
    monkeypatch.setattr(executor, "snapshot_staging", lambda: set())
    monkeypatch.setattr(executor, "files_added_since",
                        lambda _s: [recovered] if recovered.exists()
                        else [tmp_path / "02 - Star.mp3"])
    cleanup = {"n": 0}

    def fake_cleanup(files):
        cleanup["n"] += 1
        if cleanup["n"] == 1:
            return [], ["02 - Star"]   # initial: lossy, deleted
        return [recovered], []         # retry: kept
    monkeypatch.setattr(executor, "cleanup_lossy", fake_cleanup)
    monkeypatch.setattr(executor.time, "sleep", lambda _s: None)

    executor._download_for_queue_item(item)

    assert rip_calls == ["https://play.qobuz.com/album/ALB",
                         "https://play.qobuz.com/track/2"]
    assert item["n_ok"] == 1
    assert item["n_lossy"] == 0
    assert item["n_fail"] == 0


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


def test_reimport_parked_albums_clears_successes_and_keeps_failures(monkeypatch, tmp_path):
    """Parked albums get an import-only retry on the next flush: the ones that
    import are cleared from the parking dir, the ones that still fail stay put."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.queue import executor

    staging = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)
    good = staging / cfg.BEETS_RETRY_DIR / "20260101_000000-good"
    bad = staging / cfg.BEETS_RETRY_DIR / "20260101_000001-bad"
    (good / "Good Album").mkdir(parents=True)
    (bad / "Bad Album").mkdir(parents=True)

    monkeypatch.setattr(executor, "beets_import_albums",
                        lambda dirs: "ok" if "good" in str(dirs[0]) else "error")

    assert executor._reimport_parked_albums() is True
    assert not good.exists()      # re-imported cleanly → parking dir cleared
    assert bad.exists()           # still unimportable → left parked


def test_push_progress_streams_separately_and_stays_out_of_the_log():
    from qobuz_librarian.web import jobs as jm
    job = jm.Job(kind="scan")
    sub = job.subscribe()
    job.push_progress("Scanning library", 5, 10, "Beyoncé")
    line = sub.get_nowait()
    assert line.startswith(jm.PROGRESS_PREFIX)
    assert json.loads(line[len(jm.PROGRESS_PREFIX):]) == {
        "phase": "Scanning library", "current": 5, "total": 10, "item": "Beyoncé"}
    # progress is a header update, not a log line
    assert job.log_lines == []
    # a late subscriber gets the current snapshot once, so a reconnect shows
    # the live header instead of a blank bar
    snap = job.subscribe().get_nowait()
    assert snap.startswith(jm.PROGRESS_PREFIX)
