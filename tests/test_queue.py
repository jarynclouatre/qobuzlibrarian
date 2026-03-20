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


class TestBuildQueueItem:
    def _item(self, **overrides):
        defaults = dict(
            album={"id": "1", "title": "Test"},
            album_dir=Path("/music/test"),
            label="test",
            missing=[],
            present=[],
            upgrade_only=False,
            auto_upgrade=False,
        )
        defaults.update(overrides)
        return _build_queue_item(**defaults)

    def test_runtime_fields_start_at_defaults(self):
        item = self._item()
        assert item["backup_path"] is None
        assert item["n_ok"] == 0
        assert item["n_fail"] == 0
        assert item["imported"] is False
        assert item["result"] is None

    def test_siblings_to_delete_is_a_copy(self):
        original = [Path("/a"), Path("/b")]
        item = self._item(siblings_to_delete=original)
        original.append(Path("/c"))
        assert len(item["siblings_to_delete"]) == 2


class TestSerializeDeserialize:
    def _make_item(self, **overrides):
        defaults = dict(
            album={"id": "42", "title": "Round Trip"},
            album_dir=Path("/music/round-trip"),
            label="round-trip",
            missing=[{"title": "Track 1"}],
            present=[],
            upgrade_only=False,
            auto_upgrade=True,
            siblings_to_delete=[Path("/music/old-edition")],
            quality=None,
        )
        defaults.update(overrides)
        return _build_queue_item(**defaults)

    def test_round_trip_restores_album_dir(self):
        item = self._make_item()
        restored = _deserialize_queue_item(_serialize_queue_item(item))
        assert restored["album_dir"] == item["album_dir"]

    def test_round_trip_runtime_fields_reset(self):
        item = self._make_item()
        item["n_ok"] = 5
        item["imported"] = True
        restored = _deserialize_queue_item(_serialize_queue_item(item))
        assert restored["n_ok"] == 0
        assert restored["imported"] is False

    def test_round_trip_preserves_quality(self):
        item = self._make_item(quality=3)
        restored = _deserialize_queue_item(_serialize_queue_item(item))
        assert restored["quality"] == 3


class TestQueuePersistence:
    def _make_item(self, title="Test"):
        return _build_queue_item(
            album={"id": "1", "title": title},
            album_dir=Path(f"/music/{title.lower()}"),
            label=title,
            missing=[], present=[], upgrade_only=False, auto_upgrade=False,
        )

    def test_round_trip_single_item(self, tmp_path, monkeypatch):
        qfile = tmp_path / "queue.json"
        monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
        item = self._make_item(title="Album A")
        save_pending_queue([item], mode="album_walk")
        items, mode, _ = load_pending_queue()
        assert len(items) == 1
        assert items[0]["album"]["title"] == "Album A"
        assert mode == "album_walk"

    def test_load_rejects_wrong_version(self, tmp_path, monkeypatch):
        qfile = tmp_path / "queue.json"
        monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
        payload = {"version": 99, "items": [], "mode": "album_walk", "count": 0}
        qfile.write_text(json.dumps(payload))
        items, mode, _ = load_pending_queue()
        assert items is None

    def test_clear_removes_file(self, tmp_path, monkeypatch):
        qfile = tmp_path / "queue.json"
        monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
        save_pending_queue([self._make_item()], mode="album_walk")
        assert qfile.exists()
        clear_pending_queue()
        assert not qfile.exists()

    def test_save_failure_does_not_raise(self, tmp_path, monkeypatch):
        qfile = tmp_path / "nowrite" / "queue.json"
        monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
        with patch("pathlib.Path.mkdir", side_effect=OSError("no perms")):
            save_pending_queue([self._make_item()], mode="album_walk")
        assert not qfile.exists()

    def test_saved_at_is_valid_iso(self, tmp_path, monkeypatch):
        qfile = tmp_path / "queue.json"
        monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
        save_pending_queue([self._make_item()], mode="album_walk")
        _, _, saved_at = load_pending_queue()
        assert saved_at is not None
        datetime.fromisoformat(saved_at)

    def test_load_ignores_valid_json_that_is_not_an_object(self, tmp_path, monkeypatch):
        # A queue file that parses as a list/string must not crash startup.
        qfile = tmp_path / "queue.json"
        qfile.write_text('["a", "b"]', encoding="utf-8")
        monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
        assert load_pending_queue() == (None, None, None)

    def test_resume_keeps_queue_on_silent_beets_failure(self, tmp_path, monkeypatch):
        qfile = tmp_path / "queue.json"
        monkeypatch.setattr("qobuz_librarian.config.PENDING_QUEUE_FILE", qfile)
        save_pending_queue([self._make_item()], mode="walk_queue")
        assert qfile.exists()

        def fake_execute(items, args, token):
            return [], False

        monkeypatch.setattr(
            "qobuz_librarian.queue.executor._execute_download_queue", fake_execute)
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")

        offer_resume_pending_queue(Namespace(), "tok")
        assert qfile.exists()


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
