"""Tests for queue/builder.py and queue/persistence.py."""
import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_fetch.queue.builder import _build_queue_item
from qobuz_fetch.queue.persistence import (
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
        monkeypatch.setattr("qobuz_fetch.config.PENDING_QUEUE_FILE", qfile)
        item = self._make_item(title="Album A")
        save_pending_queue([item], mode="album_walk")
        items, mode, _ = load_pending_queue()
        assert len(items) == 1
        assert items[0]["album"]["title"] == "Album A"
        assert mode == "album_walk"

    def test_load_rejects_wrong_version(self, tmp_path, monkeypatch):
        qfile = tmp_path / "queue.json"
        monkeypatch.setattr("qobuz_fetch.config.PENDING_QUEUE_FILE", qfile)
        payload = {"version": 99, "items": [], "mode": "album_walk", "count": 0}
        qfile.write_text(json.dumps(payload))
        items, mode, _ = load_pending_queue()
        assert items is None

    def test_clear_removes_file(self, tmp_path, monkeypatch):
        qfile = tmp_path / "queue.json"
        monkeypatch.setattr("qobuz_fetch.config.PENDING_QUEUE_FILE", qfile)
        save_pending_queue([self._make_item()], mode="album_walk")
        assert qfile.exists()
        clear_pending_queue()
        assert not qfile.exists()

    def test_save_failure_does_not_raise(self, tmp_path, monkeypatch):
        qfile = tmp_path / "nowrite" / "queue.json"
        monkeypatch.setattr("qobuz_fetch.config.PENDING_QUEUE_FILE", qfile)
        with patch("pathlib.Path.mkdir", side_effect=OSError("no perms")):
            save_pending_queue([self._make_item()], mode="album_walk")
        assert not qfile.exists()

    def test_saved_at_is_valid_iso(self, tmp_path, monkeypatch):
        qfile = tmp_path / "queue.json"
        monkeypatch.setattr("qobuz_fetch.config.PENDING_QUEUE_FILE", qfile)
        save_pending_queue([self._make_item()], mode="album_walk")
        _, _, saved_at = load_pending_queue()
        assert saved_at is not None
        datetime.fromisoformat(saved_at)

    def test_resume_keeps_queue_on_silent_beets_failure(self, tmp_path, monkeypatch):
        """When the executor reports beets_ok=False, the resume flow must
        leave queue.json on disk so the user can retry on next launch."""
        qfile = tmp_path / "queue.json"
        monkeypatch.setattr("qobuz_fetch.config.PENDING_QUEUE_FILE", qfile)
        save_pending_queue([self._make_item()], mode="walk_queue")
        assert qfile.exists()

        def fake_execute(items, args, token):
            return [], False

        monkeypatch.setattr(
            "qobuz_fetch.queue.executor._execute_download_queue", fake_execute)
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")

        offer_resume_pending_queue(Namespace(), "tok")
        assert qfile.exists()


# ── force_track_by_track decouples repair from the ratio heuristic ─────

def test_executor_uses_per_track_urls_with_force_flag(monkeypatch):
    """11 of 14 missing → ratio 0.78 ≥ 0.7, which WOULD trigger the
    whole-album URL. With force_track_by_track the executor must hit
    exactly the 11 track URLs, never the album URL."""
    from qobuz_fetch.queue import executor

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
    monkeypatch.setattr("qobuz_fetch.api.auth.detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(executor.time, "sleep", lambda _s: None)

    executor._download_for_queue_item(item)

    assert len(seen) == 11
    assert all("/track/" in u for u in seen)
    assert not any("/album/" in u for u in seen)


# ── download_full_album heuristic boundary ─────────────────────────────

@pytest.mark.parametrize("total,missing,expect_full", [
    (100, 69, False),  # 0.69 → per-track
    (100, 70, True),   # 0.70 → full-album
    (5, 3, False),     # below the max(4, …) floor → per-track
    (5, 4, True),      # hits the floor of 4 → full-album
])
def test_download_strategy_boundary(total, missing, expect_full, monkeypatch):
    from qobuz_fetch.queue import executor

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
    monkeypatch.setattr("qobuz_fetch.api.auth.detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(executor.time, "sleep", lambda _s: None)

    executor._download_for_queue_item(item)
    assert any("/album/" in u for u in seen) is expect_full
