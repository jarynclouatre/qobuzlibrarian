"""Tests for integrations/rip.py, integrations/beets.py, integrations/lyrics.py
— the streamrip/beets seams where most real bugs live."""
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
    _FLAC_TRUNCATION_FLOOR,
    cleanup_lossy,
    cleanup_staging_residue,
    files_added_since,
    is_flac,
)

# ── rip: FLAC validation + lossy cleanup ──────────────────────────────────

def test_is_flac_rejects_truncated_keeps_complete(tmp_path, _need_ffmpeg, _need_flac):
    def _sine(path, seconds):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}",
             "-c:a", "flac", str(path)],
            check=True)

    # A short but complete track is real audio — keep it even though it sits
    # well under the size heuristic the no-flac fallback uses.
    short = tmp_path / "interlude.flac"
    _sine(short, 1.2)
    assert short.stat().st_size < _FLAC_TRUNCATION_FLOOR
    assert is_flac(short) is True

    # An interrupted download leaves a file whose header still advertises the
    # full duration, so only decoding the (missing) frames exposes the gap.
    full = tmp_path / "full.flac"
    _sine(full, 3)
    data = full.read_bytes()
    partial = tmp_path / "partial.flac"
    partial.write_bytes(data[: len(data) * 2 // 5])
    assert is_flac(partial) is False

    assert is_flac(tmp_path / "never-written.flac") is False


def test_is_flac_keeps_track_with_corrupt_embedded_art(tmp_path, _need_ffmpeg, _need_flac):
    # Streamrip embeds cover art on every track, and Qobuz's "original" art is
    # occasionally malformed. The integrity check must verify only the audio: a
    # track whose audio is intact but whose picture won't decode is a good
    # download, not a broken one, and must never be deleted.
    from mutagen.flac import FLAC, Picture
    track = tmp_path / "track.flac"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:sample_rate=44100:duration=2",
         "-c:a", "flac", str(track)], check=True)
    f = FLAC(str(track))
    pic = Picture()
    pic.type, pic.mime = 3, "image/png"
    pic.data = b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02\x03" * 64  # truncated, undecodable
    f.add_picture(pic)
    f.save()
    assert is_flac(track) is True


def test_cleanup_lossy_sorts_flac_lossy_and_broken(tmp_path):
    good = tmp_path / "good.flac"
    good.write_bytes(b"\x00" * 200_000)
    bad = tmp_path / "truncated.flac"
    bad.write_bytes(b"\x00" * 200_000)
    mp3 = tmp_path / "track.mp3"
    mp3.write_bytes(b"\x00" * 1000)
    # is_flac stubbed: only `good` verifies; the other FLAC is treated as broken.
    with patch("qobuz_librarian.integrations.rip.is_flac",
               side_effect=lambda p: p == good):
        kept, lossy, broken = cleanup_lossy([good, bad, mp3])
    assert kept == [good]
    assert lossy == ["track"] and broken == ["truncated"]
    assert not bad.exists() and not mp3.exists()


def test_files_added_since_only_returns_new_files(tmp_path, monkeypatch):
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
    existing = tmp_path / "old.flac"
    existing.write_bytes(b"x")
    new_f = tmp_path / "new.flac"
    new_f.write_bytes(b"y")
    added = files_added_since({existing})
    assert new_f in added and existing not in added


# ── rip: staging residue cleanup ─────────────────────────────────────────

def test_cleanup_staging_residue_removes_images_but_keeps_real_albums(tmp_path, monkeypatch):
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
    (tmp_path / "stray.jpg").write_bytes(b"img")
    # A dir whose name matches a residue entry but holds audio is a real album.
    album = tmp_path / "Athlete" / "Artwork"
    album.mkdir(parents=True)
    (album / "01 - Track.flac").write_bytes(b"audio data" * 1000)
    # A .cue sheet also marks a real album dir.
    cue_dir = tmp_path / "cover"
    cue_dir.mkdir()
    (cue_dir / "disc.cue").write_text("FILE track.flac WAVE")

    cleanup_staging_residue()
    assert not (tmp_path / "stray.jpg").exists()
    assert album.exists() and cue_dir.exists()


def test_cleanup_staging_residue_keeps_art_beside_leftover_audio(tmp_path, monkeypatch):
    # An interrupted run can leave a fully-downloaded album in staging; its
    # cover.jpg is the filesystem fetchart source on import (ARTWORK=sidecar).
    # The sweep must not delete residue that sits beside real audio.
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - Track.flac").write_bytes(b"audio data" * 1000)
    (album / "cover.jpg").write_bytes(b"img")
    (album / "meta.json").write_text("{}")
    # Multi-disc: art at album root, audio one level down.
    boxset = tmp_path / "Artist" / "BoxSet"
    (boxset / "Disc 1").mkdir(parents=True)
    (boxset / "Disc 1" / "01.flac").write_bytes(b"audio" * 1000)
    (boxset / "cover.jpg").write_bytes(b"img")
    # An orphan stray with no audio sibling still goes.
    orphan = tmp_path / "Old"
    orphan.mkdir()
    (orphan / "cover.jpg").write_bytes(b"img")

    cleanup_staging_residue()
    assert (album / "cover.jpg").exists() and (album / "meta.json").exists()
    assert (boxset / "cover.jpg").exists()
    assert not (orphan / "cover.jpg").exists()


def test_cleanup_staging_residue_removes_image_only_residue_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
    cover = tmp_path / "cover"
    cover.mkdir()
    (cover / "cover.jpg").write_bytes(b"img")
    cleanup_staging_residue()
    assert not cover.exists()


# ── beets: split-folder merge ─────────────────────────────────────────────

def test_merge_split_folder_moves_unique_and_skips_collisions(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "01 - a.flac").write_bytes(b"audio")      # unique → moved
    (src / "keep.flac").write_bytes(b"src_audio")
    (dst / "keep.flac").write_bytes(b"dst_audio")    # collision → dst wins, src copy kept
    moved = _merge_split_folder(dst, src)
    assert moved == 1
    assert (dst / "01 - a.flac").exists()
    assert (dst / "keep.flac").read_bytes() == b"dst_audio"


def test_merge_split_folder_dedups_identical_cover_and_removes_emptied_source(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "01 - a.flac").write_bytes(b"audio")
    (src / "cover.jpg").write_bytes(b"IMG")
    (dst / "cover.jpg").write_bytes(b"IMG")          # identical → dropped, not moved
    moved = _merge_split_folder(dst, src)
    assert moved == 1
    assert (dst / "01 - a.flac").exists()
    assert not src.exists()                          # fully emptied → removed


def test_merge_split_folder_keeps_a_differing_cover(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "cover.jpg").write_bytes(b"OLD")
    (dst / "cover.jpg").write_bytes(b"NEW")
    assert _merge_split_folder(dst, src) == 0
    assert (src / "cover.jpg").read_bytes() == b"OLD" and src.exists()


def test_merge_split_folder_repoints_beets_db(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "library.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB)")
        conn.execute("INSERT INTO items (path) VALUES (?)", (b"Artist/Album/01 - x.flac",))
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


# ── lyrics: retry manifest + signature resolution ─────────────────────────

def test_lyric_retry_round_trips_and_clears(tmp_path, monkeypatch):
    rfile = tmp_path / "retry.json"
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE", rfile)
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_VERSION", 1)
    save_lyric_retry(["/music/a.flac", "/music/b.flac"])
    assert load_lyric_retry() == ["/music/a.flac", "/music/b.flac"]
    # Saving an empty list removes the file rather than leaving an empty manifest.
    save_lyric_retry([])
    assert not rfile.exists()


def test_lyric_retry_tolerates_corrupt_or_non_object_json(tmp_path, monkeypatch):
    # load_lyric_retry runs on the dashboard + at startup — a hand-edited file
    # that parses as a list/string/number must not crash it.
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_VERSION", 1)
    for payload in ("NOT JSON", '["a", "b"]', '"a string"', "42"):
        rfile = tmp_path / "retry.json"
        rfile.write_text(payload)
        monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE", rfile)
        assert load_lyric_retry() == []


def test_resolve_signatures_to_paths_matches_on_signature(tmp_path):
    f = tmp_path / "track.flac"
    f.write_bytes(b"\x00" * 1000)
    sig = ("artist", "album", 1, 1, "title")
    with patch("qobuz_librarian.integrations.rip._flac_signature", return_value=sig):
        assert str(f) in _resolve_signatures_to_paths([(sig, "staging/track.flac")], [tmp_path])
    # A different signature on disk → no match (no false relocation).
    with patch("qobuz_librarian.integrations.rip._flac_signature",
               return_value=("artist", "album", 1, 1, "other")):
        assert _resolve_signatures_to_paths(
            [(("artist", "album", 1, 1, "wanted"), "staging/track.flac")], [tmp_path]) == []


def test_write_lyrics_saves_atomically_and_clears_legacy_tag(tmp_path):
    from mutagen.flac import VCFLACDict

    from qobuz_librarian.integrations import lyric_fetch

    real = tmp_path / "track.flac"
    real.write_bytes(b"original-audio")

    class FakeFLAC:
        def __init__(self, path):
            self.filename = str(path)
            self.tags = VCFLACDict()
            self.save_targets = []

        def save(self, target):
            self.save_targets.append(target)
            Path(target).write_bytes(b"new-audio+tags")

    f = FakeFLAC(real)
    f.tags["UNSYNCEDLYRICS"] = ["stale plain text"]
    lyric_fetch.write_lyrics(f, "[00:01.00]hello")

    assert f.tags["lyrics"] == ["[00:01.00]hello"]
    assert "unsyncedlyrics" not in f.tags
    # The live file must never be written in place — mutagen saves into a temp
    # copy that is then atomically swapped in, so a crash can't truncate it.
    assert f.save_targets and all(t != f.filename for t in f.save_targets)
    assert real.read_bytes() == b"new-audio+tags"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_lyric_state_prune_drops_entries_for_vanished_files(tmp_path):
    from qobuz_librarian.integrations import lyric_fetch
    here = tmp_path / "here.flac"
    here.write_bytes(b"x")
    gone = str(tmp_path / "gone.flac")
    state = {
        str(here): lyric_fetch.TrackState(status="synced"),
        gone: lyric_fetch.TrackState(status="synced"),
    }
    assert lyric_fetch.prune_missing(state) == 1
    assert str(here) in state and gone not in state


def test_update_state_reloads_inside_the_lock(tmp_path):
    # update_state must read the file fresh inside the lock and write the
    # mutated result — so a value another process appended before the lock was
    # acquired survives the mutation instead of being clobbered by a stale
    # in-memory snapshot.
    from qobuz_librarian.integrations import lyric_fetch
    sf = tmp_path / "state.json"
    lyric_fetch.save_state({"a": lyric_fetch.TrackState(status="synced")}, sf)
    # Simulate a concurrent writer landing a new key after the caller's last read
    # but before the prune runs.
    cur = lyric_fetch.load_state(sf)
    cur["b"] = lyric_fetch.TrackState(status="transient")
    lyric_fetch.save_state(cur, sf)

    lyric_fetch.update_state(lambda s: s.pop("a", None), sf)
    out = lyric_fetch.load_state(sf)
    assert "a" not in out          # the mutation applied
    assert "b" in out              # the concurrent writer's key was not clobbered


# ── beets: override YAML safety + dedup detection ──────────────────────────

def test_yaml_sq_quotes_safely_and_blocks_injection():
    import yaml
    value = "/music/Some Artist/Album: Subtitle"
    assert yaml.safe_load("paths:\n  default: " + _yaml_sq(value) + "\n") == {
        "paths": {"default": value}}
    # A value crafted to break out of the quote must not inject a key.
    evil = "x'\ninjected: pwned\n#"
    assert "injected" not in yaml.safe_load(
        "paths:\n  default: " + _yaml_sq(evil) + "\n")


def test_duplicate_album_dirs_targets_repeats_not_distinct_albums():
    sep = _ALBUM_FIELD_SEP
    listing = "\n".join([
        f"/music/Aphex Twin/Windowlicker (1999){sep}Aphex Twin{sep}Windowlicker",
        f"/music/Aphex Twin/Windowlicker (1999){sep}Aphex Twin{sep}Windowlicker",
        f"/music/Sigur Ros/Von (1997){sep}Sigur Ros{sep}Von",
        # One folder holding two genuinely different albums must NOT be merged.
        f"/music/V/Split{sep}A{sep}First",
        f"/music/V/Split{sep}B{sep}Second",
    ])
    assert _duplicate_album_dirs(listing) == ["/music/Aphex Twin/Windowlicker (1999)"]


def test_consolidate_skips_reimport_when_remove_fails(tmp_path, monkeypatch):
    # A failed `beet remove` leaves the rows; importing on top would make the
    # split worse, so the re-import is skipped.
    from qobuz_librarian.integrations import beets
    sep = _ALBUM_FIELD_SEP
    listing = f"/music/A/Dup (2001){sep}A{sep}Dup\n/music/A/Dup (2001){sep}A{sep}Dup"
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


def test_consolidate_runs_only_when_requested(tmp_path, monkeypatch):
    # A brand-new import (consolidate=False) skips the full-library de-dup fold;
    # an import that touched an existing folder still runs it.
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets
    cfgdir = tmp_path / "beetsconf"
    cfgdir.mkdir()
    (cfgdir / "config.yaml").write_text("directory: /music\n")
    monkeypatch.setattr(cfg, "BEETS_CONFIG_DIR", cfgdir)
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", tmp_path / "lib.db")
    monkeypatch.setattr(cfg, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(beets.shutil, "which", lambda _n: "/usr/bin/beet")
    monkeypatch.setattr(beets, "_prepare_staging_tags", lambda **_: [])
    monkeypatch.setattr(beets, "_beets_direct", lambda *a, **k: (True, "ok"))
    calls = []
    monkeypatch.setattr(beets, "_consolidate_duplicate_albums", lambda: calls.append(1))
    assert beets.beets_import_paths(consolidate=False) is True
    assert calls == []
    assert beets.beets_import_paths(consolidate=True) is True
    assert calls == [1]


# ── rip: process-group kill ───────────────────────────────────────────────

def test_kill_process_group_only_killpgs_a_separate_session():
    from qobuz_librarian.integrations import rip
    proc = MagicMock(pid=4242)
    # Child in its own session → kill the whole group.
    with patch("os.getpgid", return_value=9999), patch("os.getpgrp", return_value=1000), \
         patch("os.killpg") as killpg:
        rip._kill_process_group(proc)
    killpg.assert_called_once()

    # Child shares the caller's group (setsid hadn't run) → kill only the proc,
    # never killpg our own group.
    proc2 = MagicMock(pid=4242)
    with patch("os.getpgid", return_value=1000), patch("os.getpgrp", return_value=1000), \
         patch("os.killpg") as killpg:
        rip._kill_process_group(proc2)
    killpg.assert_not_called()
    proc2.kill.assert_called_once()


# ── beets: _beets_direct behaviour ─────────────────────────────────────────

def test_beets_direct_kills_an_idle_import(monkeypatch):
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
    ok, kind = beets._beets_direct(None, lambda: cleaned.append(True))
    assert ok is False and kind == "timeout"
    assert killed == [True] and cleaned == [True]


def test_beets_direct_detects_silent_skip_by_unmoved_audio(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets
    captured_env = {}

    class _Proc:
        def __init__(self, lines=(), on_wait=None):
            self.stdout = iter(lines)
            self.returncode = 0
            self._on_wait = on_wait

        def wait(self, timeout=None):
            if self._on_wait:
                self._on_wait()
            return 0

        def kill(self):
            pass

    def _popen_returning(proc):
        def _popen(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return proc
        return _popen

    monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)
    album = tmp_path / "Artist - Album"
    album.mkdir()
    track = album / "01.flac"
    track.write_bytes(b"flac-bytes")

    # beets moves the staged track into the library (here, deletes it) and
    # prints a per-item "Skipping." for a duplicate. The album still imported,
    # so the skip line must not flip the result to failure.
    monkeypatch.setattr(subprocess, "Popen",
                        _popen_returning(_Proc(["Skipping.\n"], track.unlink)))
    ok, kind = beets._beets_direct(None, lambda: None, [str(album)])
    assert ok is True and kind == "ok"
    assert captured_env.get("BEETSDIR") == str(cfg.BEETS_CONFIG_DIR)

    # beets exits 0 but moves nothing out of staging — the real silent skip.
    track.write_bytes(b"flac-bytes")
    monkeypatch.setattr(subprocess, "Popen", _popen_returning(_Proc()))
    ok, kind = beets._beets_direct(None, lambda: None, [str(album)])
    assert ok is False and kind == "error"


def test_beets_direct_reports_per_album_progress(monkeypatch):
    # The web progress card would otherwise sit on "Importing into your library"
    # for the whole import; beets' staging-path echoes get turned into
    # report_progress() calls naming the current album, de-duped on repeats.
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets
    from qobuz_librarian.ui_cli import logging as ql_logging
    staging = str(cfg.STAGING_DIR).rstrip("/")

    class TwoAlbumProc:
        stdout = iter([
            f"{staging}/Four Tet - There Is Love In You (3 items)\n",
            "Tagging:\n",
            "    Four Tet - There Is Love In You\n",
            f"{staging}/Four Tet - There Is Love In You (3 items)\n",  # echo
            f"{staging}/Bonobo - Black Sands (2 items)\n",
            "Tagging:\n",
            "    Bonobo - Black Sands\n",
        ])
        returncode = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    events = []
    ql_logging.set_progress_reporter(lambda phase, c, t, item: events.append((phase, c, t, item)))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: TwoAlbumProc())
    monkeypatch.setattr(beets, "clear_scan_caches", lambda: None)
    try:
        beets._beets_direct(None, lambda: None,
                            paths=[f"{staging}/Four Tet - There Is Love In You",
                                   f"{staging}/Bonobo - Black Sands"])
    finally:
        ql_logging.set_progress_reporter(None)
    items = [e[3] for e in events if e[3]]
    assert "Four Tet - There Is Love In You" in items and "Bonobo - Black Sands" in items
    assert items.count("Four Tet - There Is Love In You") == 1   # echo not double-counted
    assert 2 in {e[2] for e in events}                            # total reflects paths passed


def test_report_staging_remnants_lists_album_folders_with_counts(tmp_path, monkeypatch, caplog):
    import logging

    from qobuz_librarian.integrations import beets
    staging = tmp_path / "staging"
    (staging / "Artist One - Album A").mkdir(parents=True)
    (staging / "Artist One - Album A" / "01.flac").write_bytes(b"a")
    (staging / "Artist One - Album A" / "02.flac").write_bytes(b"b")
    (staging / "Artist Two - Album B").mkdir(parents=True)
    (staging / "Artist Two - Album B" / "01.flac").write_bytes(b"c")
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)
    monkeypatch.setattr("qobuz_librarian.config.AUDIO_EXTS", [".flac"])
    with caplog.at_level(logging.INFO, logger="qobuz_librarian"):
        beets._report_staging_remnants()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Artist One - Album A" in text and "2 track(s)" in text
    assert "Artist Two - Album B" in text and "1 track(s)" in text


# ── executor: lyric-hook crash captures signatures for post-import retry ──

def test_lyric_hook_failure_captures_signatures_not_staging_paths(tmp_path, monkeypatch):
    import types

    from qobuz_librarian.queue import executor
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "track.flac").write_bytes(b"\x00" * 100)
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)
    monkeypatch.setattr(executor, "_run_lyric_hook",
                        lambda _d: (_ for _ in ()).throw(RuntimeError("hook crash")))
    monkeypatch.setattr("qobuz_librarian.integrations.rip._flac_signature", lambda p: "SIG")
    # Signatures survive the beets move; staging paths would go stale once
    # beets relocates the files.
    sigs = executor._pre_import_staging_hooks(types.SimpleNamespace(no_compress=True))
    assert sigs == [("SIG", str(staging / "track.flac"))]


# ── beets: staging tag prep (quarantine + normalize) ──────────────────────

def test_prepare_staging_tags_skips_everything_without_mutagen(tmp_path, monkeypatch):
    # With no mutagen, every tag read fails; treating that as "untagged" would
    # quarantine the whole download and hand beets an empty staging dir.
    from qobuz_librarian.integrations import beets
    staging = tmp_path / "staging"
    (staging / "Album").mkdir(parents=True)
    f = staging / "Album" / "01.flac"
    f.write_bytes(b"flac-ish bytes")
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(beets, "HAVE_MUTAGEN", False)
    assert beets._prepare_staging_tags() == []
    assert f.exists()


def test_staging_orphan_move_preserves_same_named_files(tmp_path, monkeypatch):
    # Two albums with an identically-named leftover must both survive — a flat
    # basename move would overwrite one.
    import types

    from qobuz_librarian.integrations import beets
    staging = tmp_path / "staging"
    (staging / "AlbumA").mkdir(parents=True)
    (staging / "AlbumB").mkdir(parents=True)
    (staging / "AlbumA" / "cover.jpg").write_bytes(b"A")
    (staging / "AlbumB" / "cover.jpg").write_bytes(b"B")
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", staging)
    monkeypatch.setattr("qobuz_librarian.integrations.rip.cleanup_staging_residue", lambda: 0)
    monkeypatch.setattr("qobuz_librarian.integrations.lyrics._run_lyric_hook",
                        lambda *_a, **_k: ({}, []))
    monkeypatch.setattr(beets, "beets_import_paths", lambda *a, **k: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "1")
    beets.staging_preflight(types.SimpleNamespace(yes=False, no_downsample=True, no_compress=True))
    orphans = list((staging.parent / ".staging.orphans").rglob("cover.jpg"))
    assert len(orphans) == 2 and {p.read_bytes() for p in orphans} == {b"A", b"B"}


@pytest.fixture
def _need_ffmpeg():
    import shutil
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")


@pytest.fixture
def _need_flac():
    import shutil
    if shutil.which("flac") is None:
        pytest.skip("flac not available")


def _make_silent_flac(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=stereo", "-t", "1", "-c:a", "flac", str(path)],
        check=True)


def test_prepare_staging_tags_sets_aside_untagged_keeps_tagged(tmp_path, monkeypatch, _need_ffmpeg):
    # A cancelled/crashed rip leaves untagged FLACs beets would file under
    # '/_/'. They're moved out of the import set — but set aside, never deleted.
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
    _make_silent_flac(tagged)
    _make_silent_flac(untagged)
    f = FLAC(str(tagged))
    f["albumartist"], f["album"], f["title"] = ["Real Artist"], ["Real Album"], ["Good"]
    f.save()
    broken = staging / "Broken" / "x.flac"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a flac at all")

    moved = beets._prepare_staging_tags()
    assert tagged.exists()
    assert not untagged.exists() and untagged in moved
    assert not broken.exists() and broken in moved
    assert len(list((data / ".untagged_staging").rglob("*.flac"))) == 2


def test_prepare_staging_tags_trims_space_but_keeps_quotes(tmp_path, monkeypatch, _need_ffmpeg):
    # streamrip writes tags from its own fetch, so 'Hunky Dory ' survives into
    # the folder unless trimmed before beets import. A genuinely quoted title
    # like '"Heroes"' is kept intact — the quotes are part of the name.
    from mutagen.flac import FLAC

    from qobuz_librarian.integrations import beets
    monkeypatch.setattr("qobuz_librarian.config.STAGING_DIR", tmp_path)
    flac = tmp_path / "David Bowie" / "Hunky Dory" / "01.flac"
    _make_silent_flac(flac)
    f = FLAC(str(flac))
    f["album"], f["albumartist"], f["title"] = ["Hunky Dory "], ["David Bowie"], ['"Heroes"']
    f.save()
    beets._prepare_staging_tags()
    out = FLAC(str(flac))
    assert out["album"] == ["Hunky Dory"] and out["title"] == ['"Heroes"']


# ── beets: artwork override YAML ──────────────────────────────────────────

def _build_artwork_yaml(monkeypatch, **cfgvals):
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


def test_import_override_artwork_modes(monkeypatch):
    # sidecar (default): leave config plugins alone, no embedart.
    y = _build_artwork_yaml(monkeypatch, ARTWORK="sidecar")
    assert "embedart" not in y and "plugins:" not in y
    # embed: add embedart + fetchart, remove the file after.
    y = _build_artwork_yaml(monkeypatch, ARTWORK="embed")
    assert "embedart:" in y and "remove_art_file: yes" in y and "fetchart" in y
    # both: embed but keep the file.
    y = _build_artwork_yaml(monkeypatch, ARTWORK="both")
    assert "remove_art_file: no" in y


def test_import_override_combines_user_plugins_and_keeps_inline(monkeypatch):
    # A custom plugin list must still combine with embedart/fetchart and keep
    # the inline plugin (the seeded multi-disc path field depends on it).
    y = _build_artwork_yaml(monkeypatch, ARTWORK="embed", BEETS_PLUGINS=["lastgenre"])
    assert "lastgenre" in y and "fetchart" in y and "embedart" in y
    y = _build_artwork_yaml(monkeypatch, BEETS_PLUGINS=["lastgenre"])
    assert "inline" in y


def test_import_override_pins_autotag_off(monkeypatch):
    # Streamrip already wrote authoritative Qobuz tags. The override has to pin
    # autotag off so a user config.yaml with autotag:yes can't push downloads
    # through MusicBrainz matching, which skips every unmatched album under
    # quiet mode and strands the files in staging.
    import yaml
    conf = yaml.safe_load(_build_artwork_yaml(monkeypatch))
    assert conf["import"]["autotag"] is False


def test_import_override_pins_duplicate_action_merge(monkeypatch):
    # OUR importer must pin duplicate_action: merge regardless of the user's
    # config. `remove` would delete the existing library album on a collision
    # (irreversible); `skip` would silently import nothing for a per-track
    # gap-fill (which relies on beets MERGING the missing tracks into the
    # existing folder). merge is non-destructive and what gap-fill / the
    # consolidation re-import need.
    import yaml
    conf = yaml.safe_load(_build_artwork_yaml(monkeypatch))
    assert conf["import"]["duplicate_action"] == "merge"


def test_library_lyrics_walk_targets_library_flacs_only(tmp_path, monkeypatch):
    # The backfill must lyric the real library and leave the staging dir,
    # dot-folders and empty artists alone — fetching staging files would lyric
    # half-imported downloads behind the import hook's back.
    from collections import Counter

    import qobuz_librarian.config as cfg
    from qobuz_librarian.integrations import lyric_fetch
    from qobuz_librarian.library import lyrics as liblyr
    from qobuz_librarian.library import scanner

    music = tmp_path / "music"
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "STAGING_DIR", music / "staging")
    scanner.clear_scan_caches()

    def touch(rel):
        p = music / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return p

    expected = {
        touch("ArtistA/Album1 (2020)/01.flac"),
        touch("ArtistA/Album1 (2020)/02.flac"),
        touch("ArtistB/Album2/Disc 1/03.flac"),
    }
    touch(".hidden/x.flac")          # dot-folder artist
    touch("staging/junk.flac")       # in-progress import
    (music / "EmptyArtist").mkdir()  # no audio

    seen = {}
    monkeypatch.setattr(lyric_fetch, "index_existing", lambda *a, **k: Counter())

    def fake_fetch(paths, **kw):
        seen["paths"] = [Path(p) for p in paths]
        return Counter({"already-synced": len(seen["paths"])})

    monkeypatch.setattr(lyric_fetch, "fetch_for_paths", fake_fetch)

    res = liblyr.run_library_lyrics()
    assert set(seen["paths"]) == expected
    assert res["total"] == 3

    # Passing artist_dirs scopes the walk to those dirs only — the per-artist
    # Lyrics tool relies on this to avoid re-iterating every other artist's
    # folder. ArtistB's dir is the one we'd pick; ArtistA's tracks must drop.
    seen.clear()
    res = liblyr.run_library_lyrics(artist_dirs=[music / "ArtistB"])
    assert set(seen["paths"]) == {music / "ArtistB/Album2/Disc 1/03.flac"}
    assert res["total"] == 1
