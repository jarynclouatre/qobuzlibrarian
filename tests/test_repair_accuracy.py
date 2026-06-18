"""Real-FLAC accuracy tests for the WHOLE-LIBRARY repair scan (deep=False).

The whole-library sweep (`flows.scan_repairs`) calls
`scan_dir_for_isrc_repairs(..., deep=False)`. Before the 2026-06 fix that path
counted any not-"byte-short" FLAC as verified_ok WITHOUT decoding it, so
frame-CRC / middle-zero / partial-tail corruption with an intact STREAMINFO
sailed through and the scan reported "No damaged files found". These tests build
REAL corrupt FLACs and assert the shallow sweep actually catches them — the
decode probe is the ground truth, and a repair scan must never call a file "ok"
it never read.

Network (Qobuz) is mocked; the FLAC decode path (`flac -t` via mutagen/flac) is
real, so these are gated on ffmpeg + flac being present.
"""
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_librarian.integrations.rip import flac_audio_offset
from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs


@pytest.fixture
def _need_tools():
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")
    if shutil.which("flac") is None:
        pytest.skip("flac not available")


# ── real-FLAC builders (white noise = near-incompressible, like real music, so
#    the file stays well above the cheap byte-size gate; decode is the signal) ──

def _make_flac(path: Path, *, seconds=4, amp=0.5, isrc="USABC1234500"):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"anoisesrc=duration={seconds}:color=white:amplitude={amp}",
         "-ac", "2", "-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
         str(path)], check=True)
    from mutagen.flac import FLAC
    f = FLAC(str(path))
    if isrc:
        f["isrc"] = isrc
    f["title"] = path.stem
    f["tracknumber"] = "1"
    f.save()


def _frame_corrupt(path: Path):
    """Flip a run of bytes inside the audio frames → frame-CRC break. Size and
    STREAMINFO stay intact, so only a real decode catches it."""
    off = flac_audio_offset(str(path)) or 8192
    size = path.stat().st_size
    start = off + (size - off) // 2
    n = min(4096, size - start - 16)
    with open(path, "r+b") as fh:
        fh.seek(start)
        cur = fh.read(n)
        fh.seek(start)
        fh.write(bytes((b ^ 0xFF) for b in cur))


def _tail_truncate(path: Path, keep_frac=0.5):
    os.truncate(path, max(1, int(path.stat().st_size * keep_frac)))


def _decodes(path: Path) -> bool:
    return subprocess.run(["flac", "-t", "-s", str(path)],
                          capture_output=True).returncode == 0


def _names(entries):
    return {Path(e["path"]).name for e in entries}


# A Qobuz "match" for the corrupt files so the shallow sweep can build a
# refillable verified_truncated entry once it decides to look one up.
_QT = {"duration": 4.0, "title": "t", "track_number": 1, "isrc": "USABC1234500"}


def test_shallow_scan_catches_frame_crc_corruption(tmp_path, _need_tools):
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    p = album / "01.flac"
    _make_flac(p)
    _frame_corrupt(p)
    assert not _decodes(p), "fixture should be genuinely corrupt"

    with patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value=_QT):
        r = scan_dir_for_isrc_repairs(album, "token", deep=False)

    assert "01.flac" in _names(r["verified_truncated"]), (
        "shallow sweep must flag a frame-CRC-corrupt FLAC, not pass it as ok "
        f"(got {r})")
    assert r["verified_truncated"][0]["reason"] == "decode_failed"


def test_shallow_scan_catches_tail_truncation_above_byte_gate(tmp_path, _need_tools):
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    p = album / "01.flac"
    _make_flac(p, seconds=6)
    _tail_truncate(p, 0.5)  # 50% — well above the 15% byte-size gate
    assert not _decodes(p)

    with patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value=_QT):
        r = scan_dir_for_isrc_repairs(album, "token", deep=False)

    assert "01.flac" in _names(r["verified_truncated"]), (
        f"shallow sweep must flag a 50%-truncated FLAC (got {r})")


def test_shallow_scan_surfaces_no_isrc_corruption(tmp_path, _need_tools):
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    p = album / "01.flac"
    _make_flac(p, isrc=None)
    _frame_corrupt(p)
    assert not _decodes(p)

    r = scan_dir_for_isrc_repairs(album, "token", deep=False)
    diagnosed = {Path(e["path"]).name for e in r["no_isrc_tag"]
                 if e.get("diagnostic")}
    assert "01.flac" in diagnosed, (
        f"shallow sweep must surface a corrupt no-ISRC FLAC, not skip it (got {r})")


def test_shallow_scan_does_not_false_flag_healthy(tmp_path, _need_tools):
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    _make_flac(album / "01.flac")              # normal noise
    _make_flac(album / "02.flac", amp=0.01)    # quiet but valid
    assert _decodes(album / "01.flac") and _decodes(album / "02.flac")

    with patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value=_QT):
        r = scan_dir_for_isrc_repairs(album, "token", deep=False)

    assert r["verified_truncated"] == [], f"healthy files must not be flagged (got {r})"
    assert r["verified_ok"] == 2
