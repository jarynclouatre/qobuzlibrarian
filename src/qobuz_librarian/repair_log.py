"""Repair log and ISRC-based truncation scanner.

Two non-obvious bits of behaviour worth preserving:

- `scan_dir_for_isrc_repairs` uses a dual-gate truncation test —
  ``flen < qdur - 30s`` AND ``flen < qdur * 0.85``. Both must fire to
  flag a file. Either alone produced false positives on short tracks
  / live recordings.
- `append_repair_log` replaces pipe characters in artist/album/title
  fields with slashes (AC|DC → AC/DC) so the pipe-delimited log format
  stays unambiguously parseable.
"""
import fcntl
import os
import shutil
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.search import find_qobuz_track_by_isrc
from qobuz_librarian.integrations.rip import flac_audio_ok
from qobuz_librarian.library.scanner import read_album_dir
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log

# Lossless FLAC compresses music to roughly 0.40-0.65 of raw PCM; even
# very compressible material rarely lands below ~0.30. A file whose
# *audio portion* (file size minus metadata) is under 15% of the
# uncompressed-equivalent size for the duration STREAMINFO claims is
# almost certainly truncated — the header survives tail damage and lies
# about how much audio is actually present. The metadata-aware variant
# matters for hi-res FLAC with multi-MB embedded art: with art alone
# eating >15% of expected_uncompressed, a real partial download could
# slip past a whole-file ratio check.
_BYTE_SIZE_TRUNCATED_RATIO = 0.15


def _flac_metadata_size(path):
    """Byte offset of the first audio frame (== total metadata size,
    including the ``fLaC`` marker). Returns 0 when the file isn't a
    FLAC or the header is unreadable, so callers fall back to a
    whole-file comparison rather than a wrong answer."""
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != b"fLaC":
                return 0
            offset = 4
            while True:
                header = fh.read(4)
                if len(header) < 4:
                    return 0
                is_last = bool(header[0] & 0x80)
                size = int.from_bytes(header[1:4], "big")
                fh.seek(size, 1)
                offset += 4 + size
                if is_last:
                    return offset
    except OSError:
        return 0

# Verifying every FLAC frame's CRC costs a full read per file. Worth it for an
# explicit repair scan: the cheap size+duration gates can't see small tail-
# truncations (10 kB on a 100 MB file) or middle-zero damage, which is exactly
# the failure mode interrupted-copy corruption produces.


def _flac_decode_ok(path):
    """End-to-end FLAC integrity probe via ``flac -t`` (frame-CRC + decode).
    Returns False only on a real decode error (CRC mismatch, broken header,
    premature EOF). A missing file or a missing flac tool returns True so the
    scanner doesn't fabricate verified_truncated entries it can't stand behind.
    """
    if not path or not os.path.exists(path):
        return True
    ok = flac_audio_ok(Path(path))
    return True if ok is None else ok


def scan_dir_for_isrc_repairs(album_dir, token,
                              *, min_short_seconds=30, max_ratio=0.85,
                              deep=True):
    """Pair each FLAC in album_dir to its Qobuz recording via ISRC, then flag
    truncation by duration comparison (both gates: >30 s short AND <85% ratio).

    Returns a dict with four keys:
      verified_truncated  — ISRC match + duration short → safe to refill
      verified_ok         — ISRC match, duration normal (count, not list)
      no_isrc_tag         — no ISRC tag; recording identity unverifiable
      isrc_no_match       — ISRC tag present but Qobuz returned no match

    Only verified_truncated files are ever deleted and refilled; everything
    else is surfaced to the user without modification. ISRC identity is
    mandatory: album-edition guessing (find_qobuz_album_for_dir) can silently
    swap a 1992 master for its 2011 remaster, which is wrong for surgical repair.

    deep=True (single-album repair) verifies every track against Qobuz. A
    whole-library sweep passes deep=False: a track is only looked up on Qobuz
    when it already looks truncated from the file size + STREAMINFO alone, so a
    healthy library finishes in minutes instead of one API call per track."""
    report = {
        "verified_truncated": [],
        "verified_ok": 0,
        "no_isrc_tag": [],
        "isrc_no_match": [],
    }
    existing = read_album_dir(album_dir)
    if not existing:
        return report

    for et in existing:
        path = et.get("path") or ""
        title = et.get("title") or Path(path).stem
        isrc_raw = et.get("isrc") or ""
        isrc = isrc_raw.replace("-", "").upper().strip()
        try:
            flen = float(et.get("length") or 0)
        except (TypeError, ValueError):
            flen = 0.0

        if not isrc:
            entry = {"path": path, "title": title}
            try:
                size_bytes = os.path.getsize(path) if path else 0
            except OSError:
                size_bytes = 0
            entry["size_bytes"] = size_bytes
            # A FLAC tagless enough to be missing ISRC and small enough to
            # be obviously broken is almost certainly a damaged download.
            # Surface a friendlier hint so the user knows to hand-verify
            # rather than chasing the bland "skipped — can't verify".
            if size_bytes < 50_000:
                entry["diagnostic"] = (
                    f"likely-corrupted ({size_bytes:,} B); hand-verify "
                    "before refilling")
            report["no_isrc_tag"].append(entry)
            continue

        # Local-first truncation gate. A cut-off download leaves a FLAC whose
        # STREAMINFO still claims the full duration while the file on disk is
        # far too small — provable from the header and size alone, no network.
        # In a library sweep (deep=False) only files that trip this cheap check
        # are looked up on Qobuz to confirm and find the refill source, so a
        # healthy library finishes in minutes. deep=True verifies every track,
        # which also catches a file that decodes fine but is genuinely shorter
        # than the real recording.
        sample_rate = int(et.get("sample_rate") or 0)
        bits = int(et.get("bits") or 0)
        channels = int(et.get("channels") or 2)
        try:
            actual_size = os.path.getsize(path) if path else 0
        except OSError:
            actual_size = 0
        audio_size = max(0, actual_size - _flac_metadata_size(path)) if path else 0
        looks_byte_short = (
            sample_rate > 0 and bits > 0 and flen > 0 and audio_size > 0
            and audio_size < flen * sample_rate * channels * (bits / 8)
            * _BYTE_SIZE_TRUNCATED_RATIO)
        if not deep and not looks_byte_short:
            report["verified_ok"] += 1
            continue

        qt = find_qobuz_track_by_isrc(isrc, token)
        if qt is None:
            report["isrc_no_match"].append({
                "path": path, "title": title, "isrc": isrc,
            })
            continue

        try:
            qdur = float(qt.get("duration") or 0)
        except (TypeError, ValueError):
            qdur = 0.0
        if qdur <= 0:
            # Qobuz didn't report a duration; the byte/duration gates can't run.
            # A decode probe is duration-independent, so still catch an
            # outright-corrupt file rather than passing it as ok. (No-op when
            # the flac tool is absent — _flac_decode_ok returns True.)
            if path and not _flac_decode_ok(path):
                report["verified_truncated"].append({
                    "path": path,
                    "file_length": flen,
                    "qobuz_track": qt,
                    "qobuz_duration": qdur,
                    "isrc": isrc,
                    "title": qt.get("title") or title,
                    "track_number": qt.get("track_number") or et.get("tracknumber") or 0,
                    "reason": "decode_failed",
                })
            else:
                report["verified_ok"] += 1
            continue

        # Byte-size sanity gate against Qobuz's authoritative duration. Quiet /
        # ambient material legitimately compresses this small and decodes fine,
        # so a decode probe vetoes the flag when the flac tool is present.
        # ``audio_size`` excludes the metadata block so a multi-MB embedded
        # picture doesn't mask a truncated audio stream.
        if sample_rate > 0 and bits > 0 and audio_size > 0:
            expected_uncompressed = qdur * sample_rate * channels * (bits / 8)
            if (audio_size < expected_uncompressed * _BYTE_SIZE_TRUNCATED_RATIO
                    and not (shutil.which("flac") is not None
                             and path and _flac_decode_ok(path))):
                report["verified_truncated"].append({
                    "path": path,
                    "file_length": flen,
                    "qobuz_track": qt,
                    "qobuz_duration": qdur,
                    "isrc": isrc,
                    "title": qt.get("title") or title,
                    "track_number": qt.get("track_number") or et.get("tracknumber") or 0,
                    "actual_size": actual_size,
                    "reason": "byte_size_short",
                })
                continue

        if flen < (qdur - min_short_seconds) and flen < (qdur * max_ratio):
            report["verified_truncated"].append({
                "path": path,
                "file_length": flen,
                "qobuz_track": qt,
                "qobuz_duration": qdur,
                "isrc": isrc,
                "title": qt.get("title") or title,
                "track_number": qt.get("track_number") or et.get("tracknumber") or 0,
            })
            continue

        # Both cheap gates passed — STREAMINFO and size look fine. A 10 kB
        # tail-truncation on a 100 MB file survives both checks, as does
        # middle-zero damage where the file size is unchanged. Decode
        # probe catches frame-CRC mismatches that only show up on a
        # full read.
        if path and not _flac_decode_ok(path):
            report["verified_truncated"].append({
                "path": path,
                "file_length": flen,
                "qobuz_track": qt,
                "qobuz_duration": qdur,
                "isrc": isrc,
                "title": qt.get("title") or title,
                "track_number": qt.get("track_number") or et.get("tracknumber") or 0,
                "actual_size": actual_size,
                "reason": "decode_failed",
            })
            continue

        report["verified_ok"] += 1
    return report


_REPAIR_LOG_HEADER = (
    "# Replaced-tracks log — albums to refresh on offline clients\n"
    "#\n"
    "# Repair replaces a truncated file in place. Most music servers keep\n"
    "# the same track ID (so ratings/play counts survive), which means an\n"
    "# offline-sync client caching by ID will keep serving the old broken\n"
    "# file until you refresh that album. For each album below, on your\n"
    "# client: remove it from the offline cache, then re-download/re-sync.\n"
    "#\n"
    "# Once an entry is handled, delete its line. Append-only — anything\n"
    "# you leave behind is preserved across runs.\n"
    "#\n"
    "# Format:  YYYY-MM-DD HH:MM  |  Artist  |  Album  |  Track\n"
    "# " + ("─" * 70) + "\n"
    "\n"
)


def append_repair_log(entries):
    """Append `{artist, album, title}` rows to the replaced-tracks log
    so the user knows which albums to refresh on caching clients.

    Serializes through fcntl.flock so concurrent appenders can't interleave
    the header-check + header-write with each other's data lines — today
    the run-lock serializes everything, but the locking here keeps the
    output parseable if a future code path ever writes outside that scope.
    """
    if not entries:
        return False
    ts = time.strftime("%Y-%m-%d %H:%M")
    payload_lines = []
    for e in entries:
        artist = (e.get("artist") or "?").strip().replace("|", "/")
        album  = (e.get("album")  or "?").strip().replace("|", "/")
        title  = (e.get("title")  or "?").strip().replace("|", "/")
        payload_lines.append(f"{ts}  |  {artist}  |  {album}  |  {title}\n")
    payload = "".join(payload_lines)
    try:
        cfg.REPAIR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with cfg.REPAIR_LOG_PATH.open("a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0, 2)
            content = (_REPAIR_LOG_HEADER + payload) if f.tell() == 0 else payload
            f.write(content)
        return True
    except OSError as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Could not append to repair log ({cfg.REPAIR_LOG_PATH}): {e}"))
        return False
