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
from qobuz_librarian.integrations.rip import flac_audio_offset, flac_audio_ok
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
                              deep=True, only_isrcs=None):
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

    Either way, every FLAC is decode-probed locally (`flac -t`, no network), so
    a file is only ever counted verified_ok when it actually decodes — frame-CRC
    and middle-zero damage that leaves the size + STREAMINFO intact is caught.
    The `deep` flag controls only the Qobuz duration cross-check, not whether we
    read the file: deep=True (single-album repair) looks every track up on Qobuz
    to also catch a file that decodes fine but is genuinely shorter than the real
    recording. Both the web and CLI whole-library sweeps pass deep=True for this
    reason — a track truncated at a frame boundary with its STREAMINFO rewritten
    to the short length decodes fine and isn't byte-short, so only the duration
    cross-check catches it. deep=False stays the cheaper mode (a Qobuz call only
    on a byte-short OR won't-decode track) but the sweeps no longer use it. When
    the `flac` tool is absent a file can't be decode-checked and is counted
    `unverified`, never ok.

    only_isrcs: when given (a set of normalised ISRCs), tracks whose ISRC is not
    in the set are counted as verified_ok without an API call. _refills_intact
    uses this to confirm just the freshly-refilled tracks instead of re-verifying
    the whole album one network call per track."""
    report = {
        "verified_truncated": [],
        "verified_ok": 0,
        # The normalized ISRCs that matched a Qobuz recording AND passed the gate
        # — i.e. POSITIVELY re-verified, not merely "not flagged truncated". A
        # track whose lookup returned nothing lands in isrc_no_match, so callers
        # that must prove a refill is intact (repair backup deletion) check
        # membership here rather than absence from verified_truncated.
        "verified_ok_isrcs": set(),
        "no_isrc_tag": [],
        "isrc_no_match": [],
        # FLACs we could not decode-check because the flac tool is absent —
        # counted so the summary can say so, never reported as "verified ok".
        "unverified": 0,
    }
    existing = read_album_dir(album_dir)
    if not existing:
        return report

    for et in existing:
        path = et.get("path") or ""
        # FLAC-only scanner: the integrity probe is `flac -t` and refills are
        # re-ripped from Qobuz as FLAC. read_album_dir also returns
        # mp3/m4a/aac/ogg/opus/wav, on which `flac -t` ALWAYS fails — which
        # would flag a perfectly healthy non-FLAC as verified_truncated and
        # then delete+refill it. Skip anything that isn't FLAC.
        if not path.lower().endswith(".flac"):
            continue
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
            # A no-ISRC file can't be ISRC-refilled, but it can still be
            # *broken* — a normal-size FLAC with frame-CRC damage or a middle-
            # zero gap passes the size check yet won't decode. On a deep scan
            # (single album, or a whole-FLAC pass) probe it locally so a clean
            # non-Qobuz library still gets its corrupt files surfaced — no token
            # or ISRC needed. The sweeps now run deep, so a corrupt no-ISRC file
            # is surfaced library-wide; in the cheaper deep=False mode it still
            # trips the byte-size gate below once it has an ISRC. (No-op when the
            # flac tool is absent.)
            elif path and not _flac_decode_ok(path):
                entry["diagnostic"] = (
                    "won't decode (frame-CRC or mid-file damage); "
                    "re-download or replace from another source")
            report["no_isrc_tag"].append(entry)
            continue

        # Local-first truncation gate. A cut-off download leaves a FLAC whose
        # STREAMINFO still claims the full duration while the file on disk is
        # far too small — provable from the header and size alone, no network.
        # The cheap deep=False mode only looks a file up on Qobuz when it trips
        # this gate; deep=True — which the sweeps now use — verifies every track,
        # also catching a file that decodes fine but is genuinely shorter than
        # the real recording, at one Qobuz call per track (cached on re-scans).
        sample_rate = int(et.get("sample_rate") or 0)
        bits = int(et.get("bits") or 0)
        channels = int(et.get("channels") or 2)
        try:
            actual_size = os.path.getsize(path) if path else 0
        except OSError:
            actual_size = 0
        audio_size = max(0, actual_size - flac_audio_offset(path)) if path else 0
        looks_byte_short = (
            sample_rate > 0 and bits > 0 and flen > 0 and audio_size > 0
            and audio_size < flen * sample_rate * channels * (bits / 8)
            * _BYTE_SIZE_TRUNCATED_RATIO)
        if not deep and not looks_byte_short:
            # The cheap gates say "fine" — but frame-CRC or middle-zero damage
            # leaves the file size and STREAMINFO intact while the audio itself
            # won't decode. A repair scan must never call a file "ok" it never
            # read, so decode-probe locally (no network) before trusting it.
            # A clean file is verified_ok and stays network-free (the common,
            # fast case); a decode FAILURE falls through to the ISRC lookup +
            # flag path below so the damage is surfaced and, if matched on
            # Qobuz, refillable. A missing `flac` tool means we genuinely can't
            # verify — count it unverified rather than fabricate an "ok".
            dec = flac_audio_ok(Path(path)) if path else None
            if dec is True:
                report["verified_ok"] += 1
                report["verified_ok_isrcs"].add(isrc)
                continue
            if dec is None:
                report["unverified"] += 1
                continue
            # dec is False → genuinely corrupt; fall through to look up + flag.
        # Caller only needs a subset of ISRCs positively re-verified (e.g. the
        # post-repair integrity check on just the refilled tracks): everything
        # outside that set is counted ok without burning an API call per track.
        if only_isrcs is not None and isrc not in only_isrcs:
            report["verified_ok"] += 1
            continue

        qt = find_qobuz_track_by_isrc(isrc, token)
        if qt is None:
            entry = {"path": path, "title": title, "isrc": isrc}
            # Tagged but not on Qobuz (Apple Music rip, delisted release, …) so
            # it can't be ISRC-refilled — but a deep scan still decode-probes it
            # so a corrupt-but-unmatched file is surfaced rather than silently
            # passed. Routed to no_isrc_tag's diagnostic channel (same as a
            # broken untagged file), since the byID refill can't apply here.
            if path and not _flac_decode_ok(path):
                entry["diagnostic"] = (
                    "won't decode (frame-CRC or mid-file damage); "
                    "re-download or replace from another source")
                report["no_isrc_tag"].append(entry)
            else:
                report["isrc_no_match"].append(entry)
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
                # ISRC matched on Qobuz, file decodes; Qobuz gave no duration so
                # this is the strongest "ok" we can assert for it.
                report["verified_ok"] += 1
                report["verified_ok_isrcs"].add(isrc)
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
        report["verified_ok_isrcs"].add(isrc)
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


def _one_log_line(value):
    """Collapse a tag value to a single safe field for the pipe-delimited log:
    '|' would break the column split, and an embedded newline (legal in Vorbis
    comments / ID3 and copied straight from tags) would split the row across two
    physical lines — which read_repair_log_entries then drops both halves of."""
    return ((value or "?").strip()
            .replace("|", "/").replace("\r", " ").replace("\n", " "))


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
        artist = _one_log_line(e.get("artist"))
        album  = _one_log_line(e.get("album"))
        title  = _one_log_line(e.get("title"))
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


def read_repair_log_entries(limit=None):
    """Parse the replaced-tracks log into dicts, newest-first.

    Each data line is ``YYYY-MM-DD HH:MM  |  Artist  |  Album  |  Track``;
    header (``#`` comments) and blank lines are skipped, and lines that don't
    split into four pipe fields are dropped quietly rather than poisoning the
    view. ``limit`` caps the most recent entries (None for everything).
    """
    if not cfg.REPAIR_LOG_PATH.exists():
        return []
    entries = []
    try:
        with cfg.REPAIR_LOG_PATH.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) != 4:
                    continue
                when, artist, album, title = parts
                entries.append({"when": when, "artist": artist,
                                "album": album, "title": title})
    except OSError:
        return []
    entries.reverse()
    if limit is not None:
        entries = entries[:limit]
    return entries
