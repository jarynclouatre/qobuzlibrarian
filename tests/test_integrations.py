"""Tests for integrations/rip.py, integrations/beets.py, integrations/lyrics.py
— the streamrip/beets seams where most real bugs live."""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_librarian.integrations.beets import (
    _merge_split_folder,
)
from qobuz_librarian.integrations.lyrics import (
    load_lyric_retry,
    save_lyric_retry,
)
from qobuz_librarian.integrations.rip import (
    _FLAC_TRUNCATION_FLOOR,
    cleanup_lossy,
    cleanup_staging_residue,
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


def test_flac_audio_ok_treats_a_verify_timeout_as_broken(monkeypatch):
    # A `flac -t` that hangs past the timeout (a pathological/corrupt large FLAC)
    # must read as broken (False), not as "tool absent" (None): None routes a
    # large file through the size heuristic, which trusts it. FLAC verifies far
    # faster than real time, so a hang is the file, not the tool.
    import qobuz_librarian.integrations.rip as rip

    monkeypatch.setattr(rip.shutil, "which", lambda name: "/usr/bin/flac")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="flac", timeout=300)
    monkeypatch.setattr(rip.subprocess, "run", hang)

    assert rip.flac_audio_ok("/any/large.flac") is False


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


# ── rip: staging residue cleanup ─────────────────────────────────────────

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


# ── beets: split-folder merge ─────────────────────────────────────────────

def test_merge_split_folder_dedups_identical_cover_and_removes_emptied_source(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "01 - a.flac").write_bytes(b"audio")
    (src / "keep.flac").write_bytes(b"src_audio")
    (dst / "keep.flac").write_bytes(b"dst_audio")    # collision → dst wins, src copy kept
    (src / "cover.jpg").write_bytes(b"IMG")
    (dst / "cover.jpg").write_bytes(b"IMG")           # identical → dropped, not moved
    moved = _merge_split_folder(dst, src)
    assert moved == 1
    assert (dst / "01 - a.flac").exists()
    assert (dst / "keep.flac").read_bytes() == b"dst_audio"


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


# ── lyrics: retry manifest + atomic writes ────────────────────────────────

def test_lyric_retry_round_trips_and_clears(tmp_path, monkeypatch):
    rfile = tmp_path / "retry.json"
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_FILE", rfile)
    monkeypatch.setattr("qobuz_librarian.config.LYRIC_RETRY_VERSION", 1)
    save_lyric_retry(["/music/a.flac", "/music/b.flac"])
    assert load_lyric_retry() == ["/music/a.flac", "/music/b.flac"]
    # Saving an empty list removes the file rather than leaving an empty manifest.
    save_lyric_retry([])
    assert not rfile.exists()


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


# ── beets: _beets_direct behaviour ─────────────────────────────────────────

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


# ── beets: staging tag prep (quarantine, never delete) ────────────────────

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


# ── beets: import override pins non-destructive duplicate handling ─────────

def test_import_override_pins_duplicate_action_merge(monkeypatch):
    # OUR importer must pin duplicate_action: merge regardless of the user's
    # config. `remove` would delete the existing library album on a collision
    # (irreversible); `skip` would silently import nothing for a per-track
    # gap-fill (which relies on beets MERGING the missing tracks into the
    # existing folder). merge is non-destructive and what gap-fill / the
    # consolidation re-import need.
    import yaml

    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import beets
    monkeypatch.setattr(cfg, "BEETS_DB_PATH", Path("/config/beets/musiclibrary.db"))
    monkeypatch.setattr(cfg, "MUSIC_ROOT", Path("/music"))
    monkeypatch.setattr(cfg, "BEETS_PATH_DEFAULT", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_SINGLETON", "")
    monkeypatch.setattr(cfg, "BEETS_PATH_COMP", "")
    monkeypatch.setattr(cfg, "BEETS_PLUGINS", [])
    monkeypatch.setattr(cfg, "ARTWORK", "sidecar")
    conf = yaml.safe_load(beets._build_import_override_yaml())
    assert conf["import"]["duplicate_action"] == "merge"
    # Streamrip already wrote authoritative Qobuz tags, so autotag must be pinned
    # off — otherwise a user's autotag:yes pushes downloads through MusicBrainz
    # matching and strands unmatched albums in staging under quiet mode.
    assert conf["import"]["autotag"] is False


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
