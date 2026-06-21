"""Real-FLAC accuracy tests for the ISRC repair scan's integrity check.

A repair scan must never call a file "ok" it never read: frame-CRC, middle-zero,
or partial-tail corruption can leave the size and STREAMINFO intact, so only a
real decode catches it. The shallow (deep=False) path is the strict case — it
decode-probes every FLAC without a Qobuz call — and these build REAL corrupt
FLACs to prove it flags them; the deep path adds the duration cross-check
(last two tests). The decode probe is the ground truth.

Network (Qobuz) is mocked; the FLAC decode path (`flac -t` via mutagen/flac) is
real, so the real-FLAC tests are gated on ffmpeg + flac being present.
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


def test_deep_scan_flags_decode_clean_but_short_via_duration(tmp_path, _need_tools):
    # The Jack's Mannequin / "Everything In Transit" ground-truth gate, exercised
    # against a REAL FLAC end to end: a file that decodes perfectly but is far
    # shorter than its real Qobuz recording (a header-consistent truncation) must
    # be flagged by the deep duration cross-check. This is the exact "decodes fine
    # but cut short" mechanism — caught by the pure length comparison, NOT the
    # byte-size or decode gate, so the entry carries no "reason" key.
    album = tmp_path / "Jack's Mannequin" / "Everything In Transit (2005)"
    album.mkdir(parents=True)
    p = album / "04 - I'm Ready.flac"
    _make_flac(p, seconds=3)                    # decodes clean, ~3s
    assert _decodes(p), "fixture must decode cleanly"

    long_qt = {"duration": 235.0, "title": "I'm Ready", "track_number": 4,
               "isrc": "USABC1234500"}
    with patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value=long_qt):
        r = scan_dir_for_isrc_repairs(album, "token", deep=True)

    flagged = {Path(e["path"]).name: e for e in r["verified_truncated"]}
    assert "04 - I'm Ready.flac" in flagged, (
        f"deep scan must flag a decode-clean but short FLAC (got {r})")
    # The pure-duration gate, not byte-size/decode — it sets no "reason".
    assert flagged["04 - I'm Ready.flac"].get("reason") is None


def test_duration_gate_abs_cap_flags_long_track_short_by_over_a_minute(monkeypatch):
    # A 10-minute track missing 69 s is still 88% of its length, so the 85% ratio
    # gate alone would wave it through; the absolute 60 s cap must flag it anyway.
    # A 40 s trim (under the cap, above 85%) stays unflagged so a small edit on a
    # long track isn't false-flagged into an overwrite. Driven through mocked
    # lengths so it needs no 10-minute fixture.
    import qobuz_librarian.repair_log as rl

    def entries(flen):
        return [{"path": "/nonexistent/01.flac", "title": "t",
                 "isrc": "USABC1234500", "length": flen,
                 "sample_rate": 44100, "bits": 16, "channels": 2}]

    qt = {"duration": 600.0, "title": "t", "track_number": 1, "isrc": "USABC1234500"}
    monkeypatch.setattr(rl, "find_qobuz_track_by_isrc", lambda i, t: qt)

    monkeypatch.setattr(rl, "read_album_dir", lambda d: entries(531.0))  # 69 s short
    flagged = rl.scan_dir_for_isrc_repairs("/album", "tok", deep=True)["verified_truncated"]
    assert len(flagged) == 1 and flagged[0].get("reason") is None

    monkeypatch.setattr(rl, "read_album_dir", lambda d: entries(560.0))  # 40 s short
    assert rl.scan_dir_for_isrc_repairs("/album", "tok", deep=True)["verified_truncated"] == []


def test_duration_gate_ignores_unreadable_length_tag(monkeypatch):
    # A healthy track whose STREAMINFO length tag is unreadable (flen=0, with a
    # positive Qobuz duration) must NOT be scored a 100%-truncation and then
    # deleted+refilled. The `flen > 0` guard on the duration gate is the only
    # thing preventing that; remove it and this flips to a false positive.
    import qobuz_librarian.repair_log as rl

    entries = [{"path": "/nonexistent/01.flac", "title": "t",
                "isrc": "USABC1234500", "length": 0.0,
                "sample_rate": 44100, "bits": 16, "channels": 2}]
    qt = {"duration": 200.0, "title": "t", "track_number": 1, "isrc": "USABC1234500"}
    monkeypatch.setattr(rl, "find_qobuz_track_by_isrc", lambda i, t: qt)
    monkeypatch.setattr(rl, "read_album_dir", lambda d: entries)

    r = rl.scan_dir_for_isrc_repairs("/album", "tok", deep=True)
    assert r["verified_truncated"] == []
    assert r["verified_ok"] == 1
