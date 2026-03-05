"""Tests for integrations/rip.py, integrations/beets.py, integrations/lyrics.py."""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qobuz_librarian.integrations.beets import (
    _ALBUM_FIELD_SEP,
    _duplicate_album_dirs,
    _merge_split_folder,
    _yaml_sq,
)
from qobuz_librarian.integrations.lyrics import (
    _resolve_signatures_to_paths,
    load_lyric_retry,
    save_lyric_retry,
)
from qobuz_librarian.integrations.rip import (
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
        with patch("qobuz_librarian.integrations.rip.is_flac", return_value=True):
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
        monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
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
        monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
        jpg = tmp_path / "cover.jpg"
        jpg.write_bytes(b"img")
        count = cleanup_staging_residue()
        assert count == 1
        assert not jpg.exists()

    def test_keeps_residue_named_album_with_audio(self, tmp_path, monkeypatch):
        """A dir whose name matches a residue entry but holds audio is a real album."""
        monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
        album = tmp_path / "Athlete" / "Artwork"
        album.mkdir(parents=True)
        (album / "01 - Track.flac").write_bytes(b"audio data" * 1000)
        (tmp_path / "stray.jpg").write_bytes(b"img")
        cleanup_staging_residue()
        assert album.exists()
        assert not (tmp_path / "stray.jpg").exists()

    def test_keeps_residue_named_dir_with_cue_file(self, tmp_path, monkeypatch):
        """A dir containing a .cue sheet is a real album dir, not residue."""
        monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
        cover = tmp_path / "cover"
        cover.mkdir()
        (cover / "disc.cue").write_text("FILE track.flac WAVE")
        cleanup_staging_residue()
        assert cover.exists()

    def test_removes_residue_named_dir_with_only_images(self, tmp_path, monkeypatch):
        """A residue-named dir containing only images is streamrip residue — remove it."""
        monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
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

    def test_drops_identical_cover_and_removes_source(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "01 - a.flac").write_bytes(b"audio")
        (src / "cover.jpg").write_bytes(b"IMG")
        (dst / "cover.jpg").write_bytes(b"IMG")
        count = _merge_split_folder(dst, src)
        assert count == 1
        assert (dst / "01 - a.flac").exists()
        assert not src.exists()

    def test_keeps_a_differing_cover(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "cover.jpg").write_bytes(b"OLD")
        (dst / "cover.jpg").write_bytes(b"NEW")
        count = _merge_split_folder(dst, src)
        assert count == 0
        assert (src / "cover.jpg").read_bytes() == b"OLD"
        assert src.exists()

    def test_repoints_beets_db_for_moved_file(self, tmp_path, monkeypatch):
        import sqlite3
        db = tmp_path / "library.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB)")
            conn.execute("INSERT INTO items (path) VALUES (?)",
                         (b"Artist/Album/01 - x.flac",))
        monkeypatch.setattr("qobuz_librarian.config.MUSIC_ROOT", tmp_path)
        monkeypatch.setattr("qobuz_librarian.config.BEETS_DB_PATH", db)
        source = tmp_path / "Artist" / "Album"
        dest = tmp_path / "Artist" / "Album (2010)"
        source.mkdir(parents=True)
        (source / "01 - x.flac").write_bytes(b"audio")
        _merge_split_folder(dest, source)
        with sqlite3.connect(str(db)) as conn:
            paths = [r[0] for r in conn.execute("SELECT path FROM items")]
        assert paths == [b"Artist/Album (2010)/01 - x.flac"]


class TestLyricRetry:
    def test_round_trip(self, tmp_path, monkeypatch):
        rfile = tmp_path / "retry.json"
        monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE", rfile)
        monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_VERSION", 1)
        save_lyric_retry(["/music/a.flac", "/music/b.flac"])
        loaded = load_lyric_retry()
        assert loaded == ["/music/a.flac", "/music/b.flac"]

    def test_save_empty_removes_file(self, tmp_path, monkeypatch):
        rfile = tmp_path / "retry.json"
        monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE", rfile)
        monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_VERSION", 1)
        save_lyric_retry(["/music/a.flac"])
        assert rfile.exists()
        save_lyric_retry([])
        assert not rfile.exists()

    def test_load_returns_empty_on_corrupt_json(self, tmp_path, monkeypatch):
        rfile = tmp_path / "retry.json"
        rfile.write_text("NOT JSON")
        monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE", rfile)
        monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_VERSION", 1)
        assert load_lyric_retry() == []

    def test_load_returns_empty_on_valid_json_that_is_not_an_object(
            self, tmp_path, monkeypatch):
        # A manifest that parses as JSON but is a list/string/number must not
        # crash load_lyric_retry — it's called on the dashboard and at startup.
        monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_VERSION", 1)
        for payload in ('["a", "b"]', '"a string"', '42'):
            rfile = tmp_path / "retry.json"
            rfile.write_text(payload)
            monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE", rfile)
            assert load_lyric_retry() == []


class TestResolveSignaturesToPaths:
    def test_matches_flac_by_signature(self, tmp_path):
        f = tmp_path / "track.flac"
        f.write_bytes(b"\x00" * 1000)
        sig = ("artist", "album", 1, 1, "title")
        with patch("qobuz_librarian.integrations.rip._flac_signature", return_value=sig):
            result = _resolve_signatures_to_paths([(sig, "staging/track.flac")], [tmp_path])
        assert str(f) in result

    def test_unmatched_signature_returns_empty(self, tmp_path):
        f = tmp_path / "track.flac"
        f.write_bytes(b"\x00" * 1000)
        sig_wanted = ("artist", "album", 1, 1, "wanted")
        sig_actual = ("artist", "album", 1, 1, "other")
        with patch("qobuz_librarian.integrations.rip._flac_signature", return_value=sig_actual):
            result = _resolve_signatures_to_paths([(sig_wanted, "staging/track.flac")], [tmp_path])
        assert result == []


class TestWriteLyrics:

    def _fake_flac(self):
        import importlib
        importlib.import_module("qobuz_librarian.integrations.lyrics")
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


def test_consolidation_targets_repeats_of_one_album_not_distinct_albums():
    sep = _ALBUM_FIELD_SEP
    listing = "\n".join([
        f"/music/Aphex Twin/Windowlicker (1999){sep}Aphex Twin{sep}Windowlicker",
        f"/music/Aphex Twin/Windowlicker (1999){sep}Aphex Twin{sep}Windowlicker",
        f"/music/Sigur Ros/Von (1997){sep}Sigur Ros{sep}Von",
        # one folder that (abnormally) holds two genuinely different albums:
        # must NOT be merged, or the two would be welded into one.
        f"/music/V/Split{sep}A{sep}First",
        f"/music/V/Split{sep}B{sep}Second",
    ])
    assert _duplicate_album_dirs(listing) == ["/music/Aphex Twin/Windowlicker (1999)"]


def test_consolidation_skips_reimport_when_remove_fails(tmp_path, monkeypatch):
    # A failed `beet remove` leaves the rows in place; importing on top would
    # add a third row and make the split worse, so the re-import is skipped.
    from qobuz_librarian.integrations import beets
    sep = _ALBUM_FIELD_SEP
    listing = "\n".join([
        f"/music/A/Dup (2001){sep}A{sep}Dup",
        f"/music/A/Dup (2001){sep}A{sep}Dup",
    ])
    monkeypatch.setattr("qobuz_librarian.config.BEETS_DB_PATH", tmp_path / "lib.db")
    monkeypatch.setattr("qobuz_librarian.config.BEETS_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(beets, "_build_import_override_yaml", lambda: "")
    monkeypatch.setattr(beets.shutil, "which", lambda _b: "/usr/bin/beet")

    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        r = MagicMock()
        if "ls" in cmd:
            r.returncode, r.stdout = 0, listing
        elif "remove" in cmd:
            r.returncode, r.stdout = 1, ""   # remove fails
        else:
            r.returncode, r.stdout = 0, ""
        return r

    monkeypatch.setattr(beets.subprocess, "run", fake_run)

    beets._consolidate_duplicate_albums()

    assert any("remove" in c for c in calls)
    assert not any("import" in c for c in calls)


class TestKillProcessGroup:
    def test_group_killed_when_child_in_own_session(self):
        from qobuz_librarian.integrations import rip
        proc = MagicMock()
        proc.pid = 4242
        with patch("os.getpgid", return_value=9999), \
             patch("os.getpgrp", return_value=1000), \
             patch("os.killpg") as killpg:
            rip._kill_process_group(proc)
        killpg.assert_called_once()

    def test_does_not_killpg_own_group_before_setsid(self):
        """When child pgid matches caller's, killpg is skipped."""
        from qobuz_librarian.integrations import rip
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
        from qobuz_librarian import config as cfg
        from qobuz_librarian.integrations import beets
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
        from qobuz_librarian import config as cfg
        from qobuz_librarian.integrations import beets

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
        from qobuz_librarian.integrations import beets

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
        import logging

        from qobuz_librarian.integrations import beets

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "Artist One - Album A").mkdir()
        (staging / "Artist One - Album A" / "01.flac").write_bytes(b"a")
        (staging / "Artist One - Album A" / "02.flac").write_bytes(b"b")
        (staging / "Artist Two - Album B").mkdir()
        (staging / "Artist Two - Album B" / "01.flac").write_bytes(b"c")
        monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)
        monkeypatch.setattr("qobuz_librarian.config.AUDIO_EXTS", [".flac"])

        with caplog.at_level(logging.INFO, logger="qobuz_librarian"):
            beets._report_staging_remnants()

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "Artist One - Album A" in text
        assert "2 track(s)" in text
        assert "Artist Two - Album B" in text
        assert "1 track(s)" in text

class TestLyricHookRetryManifest:
    def test_hook_failure_records_staging_flacs(self, tmp_path, monkeypatch):
        import types

        from qobuz_librarian.queue import executor

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "track.flac").write_bytes(b"\x00" * 100)
        monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)
        monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE",
                            tmp_path / "retry.json")
        monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_VERSION", 1)
        monkeypatch.setattr(executor, "_run_lyric_hook",
                            lambda _d: (_ for _ in ()).throw(RuntimeError("hook crash")))

        args = types.SimpleNamespace(no_compress=True)
        sigs = executor._pre_import_staging_hooks(args)
        assert sigs == []

        from qobuz_librarian.integrations.lyrics import load_lyric_retry
        assert any("track.flac" in p for p in load_lyric_retry())


class TestQuarantineUntaggedStaging:
    """A cancelled or crashed rip can leave FLACs with no album/artist tags.
    beets would file them under an empty-artist '/_/' folder, so they're moved
    out of the import set before import — but set aside (recoverable), never
    deleted, since they may be salvageable."""

    @pytest.fixture(autouse=True)
    def _need_ffmpeg(self):
        import shutil
        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg not available")

    def _make_flac(self, path):
        import subprocess
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-t", "1", "-c:a", "flac", str(path)],
            check=True,
        )

    def test_untagged_and_unreadable_set_aside_tagged_kept(self, tmp_path, monkeypatch):
        from mutagen.flac import FLAC

        from qobuz_librarian.integrations import beets
        staging = tmp_path / "staging"
        data = tmp_path / "data"
        staging.mkdir()
        data.mkdir()
        monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)
        monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", data)

        tagged = staging / "Real Artist" / "Real Album" / "01 - Good.flac"
        untagged = staging / "Partial" / "00 -.flac"
        self._make_flac(tagged)
        self._make_flac(untagged)
        f = FLAC(str(tagged))
        f["albumartist"] = ["Real Artist"]
        f["album"] = ["Real Album"]
        f["title"] = ["Good"]
        f.save()
        # A file mutagen can't parse must be set aside too, never deleted.
        broken = staging / "Broken" / "x.flac"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"not a flac at all")

        moved = beets._prepare_staging_tags()

        assert tagged.exists()                              # tagged → kept
        assert not untagged.exists() and untagged in moved  # untagged → moved
        assert not broken.exists() and broken in moved      # unreadable → moved
        survivors = list((data / ".untagged_staging").rglob("*.flac"))
        assert len(survivors) == 2                          # both recoverable


def test_quarantine_skips_everything_when_mutagen_absent(tmp_path, monkeypatch):
    # With no mutagen every tag read fails; treating that as "untagged" would
    # quarantine the whole download and hand beets an empty staging dir.
    from qobuz_librarian.integrations import beets
    staging = tmp_path / "staging"
    (staging / "Album").mkdir(parents=True)
    f = staging / "Album" / "01.flac"
    f.write_bytes(b"flac-ish bytes")
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(beets, "HAVE_MUTAGEN", False)

    moved = beets._prepare_staging_tags()

    assert moved == []
    assert f.exists()


def test_staging_orphan_move_preserves_same_named_files(tmp_path, monkeypatch):
    # Two albums with an identically-named leftover must both survive in the
    # orphan dir — a flat basename move would overwrite one.
    import types

    from qobuz_librarian.integrations import beets
    staging = tmp_path / "staging"
    (staging / "AlbumA").mkdir(parents=True)
    (staging / "AlbumB").mkdir(parents=True)
    (staging / "AlbumA" / "cover.jpg").write_bytes(b"A")
    (staging / "AlbumB" / "cover.jpg").write_bytes(b"B")
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)

    monkeypatch.setattr("qobuz_librarian.integrations.rip.cleanup_staging_residue",
                        lambda: 0)
    monkeypatch.setattr("qobuz_librarian.integrations.lyrics._run_lyric_hook",
                        lambda *_a, **_k: ({}, []))
    # beets "succeeds" but leaves the unimportable files in staging.
    monkeypatch.setattr(beets, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "1")

    args = types.SimpleNamespace(yes=False, no_downsample=True, no_compress=True)
    beets.staging_preflight(args)

    orphans = list((staging.parent / ".staging.orphans").rglob("cover.jpg"))
    assert len(orphans) == 2
    assert {p.read_bytes() for p in orphans} == {b"A", b"B"}


class TestNormalizeStagingTags:
    """streamrip writes tags from its own Qobuz fetch, so trailing whitespace
    ('Hunky Dory ') and literal outer quotes ('\"Heroes\"') survive into the
    on-disk folder unless the staged tags are cleaned before beets import."""

    @pytest.fixture(autouse=True)
    def _need_ffmpeg(self):
        import shutil
        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg not available")

    def _make_flac(self, path):
        import subprocess
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-t", "1", "-c:a", "flac", str(path)],
            check=True,
        )

    def test_trailing_space_and_quotes_stripped(self, tmp_path, monkeypatch):
        from mutagen.flac import FLAC

        from qobuz_librarian.integrations import beets
        monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)

        flac = tmp_path / "David Bowie" / "Hunky Dory" / "01.flac"
        self._make_flac(flac)
        f = FLAC(str(flac))
        f["album"] = ["Hunky Dory "]
        f["albumartist"] = ["David Bowie"]
        f["title"] = ['"Heroes"']
        f.save()

        beets._prepare_staging_tags()

        out = FLAC(str(flac))
        assert out["album"] == ["Hunky Dory"]
        assert out["title"] == ["Heroes"]
        assert out["albumartist"] == ["David Bowie"]


class TestImportOverrideArtwork:
    """ARTWORK picks where cover art goes: a file (sidecar/fetchart), embedded
    in the tracks (embed/embedart), or both."""

    def _build(self, monkeypatch, **cfgvals):
        from qobuz_librarian import config as cfg
        from qobuz_librarian.integrations import beets
        monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("/config/beets/musiclibrary.db"))
        monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))
        monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "")
        monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "")
        monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
        monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
        monkeypatch.setattr(cfg, "ARTWORK", "sidecar")
        for k, v in cfgvals.items():
            monkeypatch.setattr(cfg, k, v)
        return beets._build_import_override_yaml()

    def test_sidecar_default_leaves_config_plugins_alone(self, monkeypatch):
        y = self._build(monkeypatch, ARTWORK="sidecar")
        assert "embedart" not in y
        assert "plugins:" not in y

    def test_embed_adds_embedart_and_drops_file(self, monkeypatch):
        y = self._build(monkeypatch, ARTWORK="embed")
        assert "embedart:" in y and "remove_art_file: yes" in y
        assert "fetchart" in y and "embedart" in y

    def test_both_keeps_the_file(self, monkeypatch):
        y = self._build(monkeypatch, ARTWORK="both")
        assert "remove_art_file: no" in y

    def test_embed_combines_with_user_plugins(self, monkeypatch):
        y = self._build(monkeypatch, ARTWORK="embed", BEETS_PLUGINS=["lastgenre"])
        assert "lastgenre" in y and "fetchart" in y and "embedart" in y

    def test_inline_kept_when_plugin_list_replaced(self, monkeypatch):
        # The seeded path template's multi-disc field comes from inline; a
        # custom plugin list must not drop it.
        y = self._build(monkeypatch, BEETS_PLUGINS=["lastgenre"])
        assert "inline" in y


def test_consolidate_runs_only_when_requested(tmp_path, monkeypatch):
    """A brand-new import passes consolidate=False, so the full-library de-dup
    fold (a `beet ls -a` over everything) is skipped; an import that touched an
    existing folder still runs it."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets

    cfgdir = tmp_path / "beetsconf"
    cfgdir.mkdir()
    (cfgdir / "config.yaml").write_text("directory: /music\n")
    monkeypatch.setattr(cfg, "BEETS_CONFIG_DIR", cfgdir)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", tmp_path / "lib.db")
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(beets.shutil, "which", lambda _n: "/usr/bin/beet")
    monkeypatch.setattr(beets, "_prepare_staging_tags", lambda: [])
    monkeypatch.setattr(beets, "_beets_direct", lambda *a, **k: True)

    calls = []
    monkeypatch.setattr(beets, "_consolidate_duplicate_albums",
                        lambda: calls.append(1))

    assert beets.beets_import_paths(consolidate=False) is True
    assert calls == []
    assert beets.beets_import_paths(consolidate=True) is True
    assert calls == [1]
