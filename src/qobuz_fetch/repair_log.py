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
import time
from pathlib import Path

from qobuz_fetch import config as cfg
from qobuz_fetch.api.search import find_qobuz_track_by_isrc
from qobuz_fetch.library.scanner import read_album_dir
from qobuz_fetch.ui_cli.colors import C, fmt
from qobuz_fetch.ui_cli.logging import log


def scan_dir_for_isrc_repairs(album_dir, token,
                              *, min_short_seconds=30, max_ratio=0.85):
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
    swap a 1992 master for its 2011 remaster, which is wrong for surgical repair."""
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
            report["no_isrc_tag"].append({"path": path, "title": title})
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
            # Qobuz didn't report a duration; can't compare. Treat as "ok"
            # rather than fabricate a flag. Real truncations almost always
            # come with a non-zero Qobuz duration on the matched record.
            report["verified_ok"] += 1
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
        else:
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
