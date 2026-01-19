"""Tests for integrations/rip.py, integrations/beets.py, integrations/lyrics.py."""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qobuz_fetch.integrations.beets import _merge_split_folder, _yaml_sq
from qobuz_fetch.integrations.lyrics import (
    _resolve_signatures_to_paths,
    load_lyric_retry,
    save_lyric_retry,
)
from qobuz_fetch.integrations.rip import (
    cleanup_lossy,
    cleanup_staging_residue,
    files_added_since,
    is_flac,
    snapshot_staging,
)


class TestIsFlac:
    def test_small_file_returns_false(self, tmp_path):
        f = tmp_path / "tiny.flac"
        f.write_bytes(b"\x00" * 100)
        assert is_flac(f) is False

    def test_truncated_50kb_rejected_when_ffprobe_missing(self, tmp_path):
        # 50KB is under the 150KB floor, so it must be rejected even without ffprobe.
        f = tmp_path / "truncated.flac"
        f.write_bytes(b"\x00" * 50_000)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert is_flac(f) is False

    def test_valid_flac_returns_true(self, tmp_path):
        f = tmp_path / "good.flac"
        f.write_bytes(b"\x00" * 200_000)
        ffprobe_out = json.dumps({
            "streams": [{"codec_name": "flac"}],
            "format": {"duration": "3.5"},
        })
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ffprobe_out
        with patch("subprocess.run", return_value=mock_result):
            assert is_flac(f) is True


class TestCleanupLossy:
    def test_keeps_valid_flac(self, tmp_path):
        f = tmp_path / "track.flac"
        f.write_bytes(b"\x00" * 200_000)
        with patch("qobuz_fetch.integrations.rip.is_flac", return_value=True):
            kept, deleted = cleanup_lossy([f])
        assert f in kept

    def test_deletes_mp3(self, tmp_path):
        f = tmp_path / "track.mp3"
        f.write_bytes(b"\x00" * 1000)
        kept, deleted = cleanup_lossy([f])
        assert kept == []
        assert not f.exists()


class TestStagingSnapshot:
    def test_files_added_since_finds_new_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("qobuz_fetch.config.STAGING_DIR", tmp_path)
        existing = tmp_path / "old.flac"
        existing.write_bytes(b"x")
        prior = {existing}
        new_f = tmp_path / "new.flac"
        new_f.write_bytes(b"y")
        added = files_added_since(prior)
        assert new_f in added
        assert existing not in added


class TestCleanupStagingResidue:
    def test_removes_jpg_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("qobuz_fetch.config.STAGING_DIR", tmp_path)
        jpg = tmp_path / "cover.jpg"
        jpg.write_bytes(b"img")
        count = cleanup_staging_residue()
        assert count == 1
        assert not jpg.exists()

    def test_keeps_residue_named_album_with_audio(self, tmp_path, monkeypatch):
        """A dir whose name matches a residue entry but holds audio is a real album."""
        monkeypatch.setattr("qobuz_fetch.config.STAGING_DIR", tmp_path)
        album = tmp_path / "Athlete" / "Artwork"
        album.mkdir(parents=True)
        (album / "01 - Track.flac").write_bytes(b"audio data" * 1000)
        (tmp_path / "stray.jpg").write_bytes(b"img")
        cleanup_staging_residue()
        assert album.exists()
        assert not (tmp_path / "stray.jpg").exists()

    def test_keeps_residue_named_dir_with_cue_file(self, tmp_path, monkeypatch):
        """A dir containing a .cue sheet is a real album dir, not residue."""
        monkeypatch.setattr("qobuz_fetch.config.STAGING_DIR", tmp_path)
        cover = tmp_path / "cover"
        cover.mkdir()
        (cover / "disc.cue").write_text("FILE track.flac WAVE")
        cleanup_staging_residue()
        assert cover.exists()

    def test_removes_residue_named_dir_with_only_images(self, tmp_path, monkeypatch):
        """A residue-named dir containing only images is streamrip residue — remove it."""
        monkeypatch.setattr("qobuz_fetch.config.STAGING_DIR", tmp_path)
        cover = tmp_path / "cover"
        cover.mkdir()
        (cover / "cover.jpg").write_bytes(b"img")
        cleanup_staging_residue()
        assert not cover.exists()


class TestMergeSplitFolder:
    def test_moves_files(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        (src / "track.flac").write_bytes(b"audio")
        count = _merge_split_folder(dst, src)
        assert count == 1
        assert (dst / "track.flac").exists()

    def test_skips_existing_destination(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "track.flac").write_bytes(b"src_audio")
        (dst / "track.flac").write_bytes(b"dst_audio")
        count = _merge_split_folder(dst, src)
        assert count == 0
        assert (dst / "track.flac").read_bytes() == b"dst_audio"


class TestLyricRetry:
    def test_round_trip(self, tmp_path, monkeypatch):
        rfile = tmp_path / "retry.json"
        monkeypatch.setattr("qobuz_fetch.config.LYRIC_RETRY_FILE", rfile)
        monkeypatch.setattr("qobuz_fetch.config.LYRIC_RETRY_VERSION", 1)
        save_lyric_retry(["/music/a.flac", "/music/b.flac"])
        loaded = load_lyric_retry()
        assert loaded == ["/music/a.flac", "/music/b.flac"]

    def test_save_empty_removes_file(self, tmp_path, monkeypatch):
        rfile = tmp_path / "retry.json"
        monkeypatch.setattr("qobuz_fetch.config.LYRIC_RETRY_FILE", rfile)
        monkeypatch.setattr("qobuz_fetch.config.LYRIC_RETRY_VERSION", 1)
        save_lyric_retry(["/music/a.flac"])
        assert rfile.exists()
        save_lyric_retry([])
        assert not rfile.exists()

    def test_load_returns_empty_on_corrupt_json(self, tmp_path, monkeypatch):
        rfile = tmp_path / "retry.json"
        rfile.write_text("NOT JSON")
        monkeypatch.setattr("qobuz_fetch.config.LYRIC_RETRY_FILE", rfile)
        monkeypatch.setattr("qobuz_fetch.config.LYRIC_RETRY_VERSION", 1)
        assert load_lyric_retry() == []


class TestResolveSignaturesToPaths:
    def test_matches_flac_by_signature(self, tmp_path):
        f = tmp_path / "track.flac"
        f.write_bytes(b"\x00" * 1000)
        sig = ("artist", "album", 1, 1, "title")
        with patch("qobuz_fetch.integrations.rip._flac_signature", return_value=sig):
            result = _resolve_signatures_to_paths([(sig, "staging/track.flac")], [tmp_path])
        assert str(f) in result

    def test_unmatched_signature_returns_empty(self, tmp_path):
        f = tmp_path / "track.flac"
        f.write_bytes(b"\x00" * 1000)
        sig_wanted = ("artist", "album", 1, 1, "wanted")
        sig_actual = ("artist", "album", 1, 1, "other")
        with patch("qobuz_fetch.integrations.rip._flac_signature", return_value=sig_actual):
            result = _resolve_signatures_to_paths([(sig_wanted, "staging/track.flac")], [tmp_path])
        assert result == []


class TestWriteLyrics:

    def _fake_flac(self):
        import importlib
        importlib.import_module("qobuz_fetch.integrations.lyrics")
        import lyric_fetch
        from mutagen.flac import VCFLACDict

        class FakeFLAC:
            def __init__(self):
                self.tags = VCFLACDict()
                self.saved = 0
            def save(self):
                self.saved += 1

        return lyric_fetch, FakeFLAC()

    def test_lyrics_actually_written_and_persisted(self):
        lyric_fetch, f = self._fake_flac()
        lyric_fetch.write_lyrics(f, "[00:01.00]hello")
        assert f.tags["lyrics"] == ["[00:01.00]hello"]
        assert f.saved == 1

    def test_legacy_unsyncedlyrics_removed_any_case(self):
        lyric_fetch, f = self._fake_flac()
        f.tags["UNSYNCEDLYRICS"] = ["stale plain text"]
        lyric_fetch.write_lyrics(f, "new synced")
        assert f.tags["lyrics"] == ["new synced"]
        assert "unsyncedlyrics" not in f.tags


# ── beets override YAML must survive quotes in path templates ─────────

def test_yaml_sq_roundtrips_through_safe_load():
    import yaml
    value = "/music/Some Artist/Album: Subtitle"
    doc = yaml.safe_load("paths:\n  default: " + _yaml_sq(value) + "\n")
    assert doc == {"paths": {"default": value}}


def test_yaml_sq_quote_cannot_inject_a_directive():
    import yaml
    evil = "x'\ninjected: pwned\n#"
    doc = yaml.safe_load("paths:\n  default: " + _yaml_sq(evil) + "\n")
    assert "injected" not in doc


class TestKillProcessGroup:
    def test_group_killed_when_child_in_own_session(self):
        from qobuz_fetch.integrations import rip
        proc = MagicMock()
        proc.pid = 4242
        with patch("os.getpgid", return_value=9999), \
             patch("os.getpgrp", return_value=1000), \
             patch("os.killpg") as killpg:
            rip._kill_process_group(proc)
        killpg.assert_called_once()

    def test_does_not_killpg_own_group_before_setsid(self):
        """When child pgid matches caller's, killpg is skipped."""
        from qobuz_fetch.integrations import rip
        proc = MagicMock()
        proc.pid = 4242
        with patch("os.getpgid", return_value=1000), \
             patch("os.getpgrp", return_value=1000), \
             patch("os.killpg") as killpg:
            rip._kill_process_group(proc)
        killpg.assert_not_called()
        proc.kill.assert_called_once()


class TestBeetsImportTimeout:
    def test_idle_import_is_killed(self, monkeypatch):
        """Zero output for BEETS_TIMEOUT → killed; cleanup_fn called."""
        from qobuz_fetch import config as cfg
        from qobuz_fetch.integrations import beets
        monkeypatch.setattr(cfg, "BEETS_TIMEOUT", 1)

        killed, cleaned = [], []

        class HungProc:
            stdout = iter(())
            returncode = -9
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="beet", timeout=timeout)
            def kill(self):
                killed.append(True)

        seq = iter([1000.0] + [9999.0] * 50)
        monkeypatch.setattr(beets.time, "monotonic", lambda: next(seq, 9999.0))
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: HungProc())
        monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)

        ok = beets._beets_direct(None, lambda: cleaned.append(True))
        assert ok is False
        assert killed == [True]
        assert cleaned == [True]


class TestBeetsDirect:
    def test_beets_direct_sets_beetsdir_env(self, monkeypatch):
        """_beets_direct must pass BEETSDIR=cfg.BEETS_CONFIG_DIR so beets
        loads /config/beets/config.yaml (autotag:no, move:yes)."""
        from qobuz_fetch import config as cfg
        from qobuz_fetch.integrations import beets

        captured_env = {}

        class OkProc:
            stdout = iter(())
            returncode = 0
            def wait(self, timeout=None): return 0
            def kill(self): pass

        def fake_popen(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return OkProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)
        beets._beets_direct(None, lambda: None)

        assert "BEETSDIR" in captured_env
        assert captured_env["BEETSDIR"] == str(cfg.BEETS_CONFIG_DIR)

    def test_silent_skip_detection_catches_skipping_dot(self, monkeypatch):
        """beets prints 'Skipping.' on quiet-mode ambiguous MB match;
        must be detected as failed import, not success."""
        from qobuz_fetch.integrations import beets

        class SkippingProc:
            stdout = iter(["Skipping.\n"])
            returncode = 0
            def wait(self, timeout=None): return 0
            def kill(self): pass

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: SkippingProc())
        monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)
        ok = beets._beets_direct(None, lambda: None)
        assert ok is False


class TestReportStagingRemnants:
    def test_lists_album_folders_with_track_counts(self, tmp_path, monkeypatch, caplog):
        """After a beets failure, the user has to know which album folders
        are stuck in staging — a bare "files remain in staging" line is
        useless when a batch of 100 albums failed mid-import."""
        import logging

        from qobuz_fetch.integrations import beets

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "Artist One - Album A").mkdir()
        (staging / "Artist One - Album A" / "01.flac").write_bytes(b"a")
        (staging / "Artist One - Album A" / "02.flac").write_bytes(b"b")
        (staging / "Artist Two - Album B").mkdir()
        (staging / "Artist Two - Album B" / "01.flac").write_bytes(b"c")
        monkeypatch.setattr("qobuz_fetch.config.STAGING_DIR", staging)
        monkeypatch.setattr("qobuz_fetch.config.AUDIO_EXTS", [".flac"])

        with caplog.at_level(logging.INFO, logger="qobuz_librarian"):
            beets._report_staging_remnants()

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "Artist One - Album A" in text
        assert "2 track(s)" in text
        assert "Artist Two - Album B" in text
        assert "1 track(s)" in text

class TestLyricHookRetryManifest:
    def test_hook_failure_records_staging_flacs(self, tmp_path, monkeypatch):
        """When _run_lyric_hook raises, _pre_import_staging_hooks must
        record the staging FLACs so the orphans can be retried later."""
        import types

        from qobuz_fetch.queue import executor

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "track.flac").write_bytes(b"\x00" * 100)
        monkeypatch.setattr("qobuz_fetch.config.STAGING_DIR", staging)
        monkeypatch.setattr("qobuz_fetch.config.LYRIC_RETRY_FILE",
                            tmp_path / "retry.json")
        monkeypatch.setattr("qobuz_fetch.config.LYRIC_RETRY_VERSION", 1)
        monkeypatch.setattr(executor, "_run_lyric_hook",
                            lambda _d: (_ for _ in ()).throw(RuntimeError("hook crash")))

        args = types.SimpleNamespace(no_compress=True)
        sigs = executor._pre_import_staging_hooks(args)
        assert sigs == []

        from qobuz_fetch.integrations.lyrics import load_lyric_retry
        assert any("track.flac" in p for p in load_lyric_retry())
