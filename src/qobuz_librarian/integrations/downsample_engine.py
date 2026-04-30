"""ffmpeg resampling engine for the downsample feature.

`downsample_dir` resamples a tree's high-rate FLACs in place; `scan_dir_for_hires`
reports what would shrink without touching anything. The higher-level discovery
(grouping into per-album review candidates) lives in library/downsample.py.

Targets (preserves integer-ratio family for clean math):
  88.2 / 176.4 / 352.8 kHz  →  44.1 kHz
  96   / 192   / 384   kHz  →  48   kHz

Quality settings:
  - Resampler: soxr at precision 28 if available, else swresample with
    filter_size=512, phase_shift=20 (very high quality).
  - Triangular high-pass dither (decorrelates quantization noise).
  - Source bit depth preserved (16-bit stays 16-bit, 24-bit stays 24-bit).
  - FLAC compression level 5 (default — lossless, fast encode).
"""
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Downsampling needs ffmpeg to resample. Gate on it so a checkout without the
# binary skips the step cleanly instead of failing per-album at run time; in the
# image ffmpeg is a pinned dependency, so this is always True there.
HAVE_DOWNSAMPLE = shutil.which("ffmpeg") is not None

MUSIC_ROOT       = Path(os.environ.get("MUSIC_ROOT", "/music"))
# 1024 is enough to clear large ID3v2 preambles on the rare track that has one.
PROBE_BYTES      = 1024
RESAMPLE_WORKERS = int(os.environ.get("RESAMPLE_WORKERS", "4"))


def human(b):
    s = "-" if b < 0 else ""
    b = abs(b)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{s}{b:.1f} {u}"
        b /= 1024
    return f"{s}{b:.1f} PB"


_RESAMPLER_FILTER = None

_SOXR_FILTER = "aresample=resampler=soxr:precision=28:dither_method=triangular_hp"
_SWR_FILTER  = "aresample=filter_size=512:phase_shift=20:dither_method=triangular_hp"


def _soxr_available():
    """True when this ffmpeg can resample through libsoxr.

    Probed by running the soxr filter on a scrap of generated audio: that
    proves the filter loads and the precision/dither options take, which a
    build-config string wouldn't. ffmpeg has no flag that lists its
    resamplers, so there's nothing cheaper to query."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "anullsrc=r=96000:cl=stereo",
             "-af", _SOXR_FILTER, "-ar", "48000", "-t", "0.05", "-f", "null", "-"],
            capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def detect_resampler_filter():
    """Pick the best resampler this ffmpeg has, as (-af filter, name).

    soxr at precision 28 when libsoxr is present, else swresample's wide
    polyphase filter. Cached after the first probe — ffmpeg's capabilities
    don't change within a run, and the per-album hook would otherwise probe
    once per album in a batch."""
    global _RESAMPLER_FILTER
    if _RESAMPLER_FILTER is None:
        _RESAMPLER_FILTER = ((_SOXR_FILTER, "soxr p28") if _soxr_available()
                             else (_SWR_FILTER, "swresample HQ"))
    return _RESAMPLER_FILTER


def parse_flac_info(data: bytes):
    """Return (sample_rate, bits_per_sample) from FLAC header bytes, or (0, 0)."""
    i = data.find(b"fLaC")
    if i < 0:
        return 0, 0
    si = i + 8  # skip fLaC(4) + metadata block header(4)
    if len(data) < si + 14:
        return 0, 0
    sr = (data[si + 10] << 12) | (data[si + 11] << 4) | (data[si + 12] >> 4)
    bps = (((data[si + 12] & 0x1) << 4) | (data[si + 13] >> 4)) + 1
    return sr, bps


def read_local_bit_depth(path: Path) -> int:
    """Bit depth of a local FLAC. metaflac reads it straight from STREAMINFO,
    immune to any ID3-style preamble that would push the header past the byte
    window; fall back to parsing the header bytes when the flac tools aren't
    installed. Returns 0 on failure. Getting this wrong upconverts the resample
    (a 24-bit master re-encoded at s32), so the read has to be reliable."""
    try:
        out = subprocess.run(
            ["metaflac", "--show-bps", str(path)],
            capture_output=True, text=True, timeout=10).stdout.strip()
        if out:
            return int(out)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, ValueError):
        pass
    try:
        with open(path, "rb") as f:
            data = f.read(PROBE_BYTES)
        _, bps = parse_flac_info(data)
        return bps
    except OSError:
        return 0


def read_sample_rate(path: Path) -> int:
    """Sample rate of a local FLAC. The 1 KB header byte-parse settles the
    common case in a single read; metaflac backstops files whose STREAMINFO
    sits past the probe window (a leading ID3 tag would do it), so a hi-res
    file isn't silently dropped from the resample set. Returns 0 on failure.

    Inverse of read_local_bit_depth: this is the bulk path (one read per file
    across the whole library), so the cheap byte read leads and the subprocess
    is the exception, not the rule."""
    try:
        with open(path, "rb") as f:
            data = f.read(PROBE_BYTES)
        sr, _ = parse_flac_info(data)
    except OSError:
        return 0
    if sr:
        return sr
    try:
        out = subprocess.run(
            ["metaflac", "--show-sample-rate", str(path)],
            capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out) if out else 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, ValueError):
        return 0


def target_rate(sr):
    if sr in (88200, 176400, 352800):
        return 44100
    if sr in (96000, 192000, 384000):
        return 48000
    return None


def _flac_audio_offset(path):
    """Byte offset of the first audio frame (the fLaC marker plus every metadata
    block). Lets a caller separate the audio stream from metadata/embedded art,
    which don't shrink on resample. Returns 0 when the file isn't a plain FLAC or
    the header is unreadable, so the caller falls back to the whole-file size."""
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
                offset += 4 + int.from_bytes(header[1:4], "big")
                fh.seek(offset)
                if is_last:
                    return offset
    except OSError:
        return 0


def scan_dir_for_hires(directory):
    """List the high-sample-rate FLACs under `directory` (recursive) that the
    resampler would shrink — without touching anything.

    Returns ``{"hires": [{"path", "sr", "target", "size", "audio_size"}],
    "n_flac": int}``. ``audio_size`` is the file minus its metadata/art (which
    don't shrink), so a saving estimate scaled off it isn't inflated by a big
    embedded cover. The downsample scan uses this to build review candidates;
    downsample_dir runs the same probe before it resamples.
    """
    directory = Path(directory)
    hires = []
    n_flac = 0
    if not directory.is_dir():
        return {"hires": hires, "n_flac": 0}
    for p in directory.rglob("*.flac"):
        if p.name.startswith(".") or not p.is_file():
            continue
        n_flac += 1
        sr = read_sample_rate(p)
        rate = target_rate(sr)
        if not rate:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        offset = _flac_audio_offset(p)
        audio_size = max(0, size - offset) if offset else size
        hires.append({"path": str(p), "sr": sr, "target": rate,
                      "size": size, "audio_size": audio_size})
    return {"hires": hires, "n_flac": n_flac}


def _decode_ok(path):
    """True if the FLAC at `path` decodes cleanly (flac -t, frame-CRC + decode).

    A missing flac tool returns True — the resample already succeeded and
    without the reference decoder there's nothing to second-guess it with; the
    source is preserved on a real failure regardless. A timeout or OS error
    returns False so an unverifiable encode never overwrites the original."""
    try:
        r = subprocess.run(["flac", "-t", "-s", str(path)],
                           capture_output=True, timeout=300,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


# Resample output is written to a dot-prefixed temp beside the source, then
# atomically swapped in. The dot keeps the library scanner from indexing it
# mid-encode; the shared prefix lets a later run recognise and sweep one an
# interrupt left behind.
_TMP_PREFIX = ".compress-"


def resample_one(rel, sr, rate, af_filter, *, base_dir=None):
    """Resample one file. Returns (rel, sr, rate, saved_bytes, error_msg).

    base_dir defaults to MUSIC_ROOT. Pass an alternate root (e.g.
    STAGING_DIR) when operating on files that have not been imported
    onto the canonical music tree yet.
    """
    src = (base_dir or MUSIC_ROOT) / rel
    # Encode to a temp file in the SAME directory as the source, then swap it
    # in with os.replace() — an atomic same-filesystem rename, so an interrupt
    # (Ctrl+C / OOM / power loss) always leaves either the intact original or
    # the fully-encoded replacement, never a half-written file. The same-dir
    # temp is what keeps the rename on one filesystem: a cross-device move
    # degrades to copy-then-unlink, where an interrupt mid-copy would destroy
    # the only lossless copy.
    tmp = None
    try:
        in_size = src.stat().st_size

        bps = read_local_bit_depth(src)
        sample_fmt = "s16" if bps == 16 else "s32"

        fd, tmp_name = tempfile.mkstemp(
            dir=str(src.parent), prefix=_TMP_PREFIX, suffix=".flac")
        os.close(fd)
        tmp = Path(tmp_name)

        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(src),
                "-af", af_filter,
                "-ar", str(rate),
                "-sample_fmt", sample_fmt,
                "-c:a", "flac",
                "-map_metadata", "0",
                "-y", str(tmp),
            ],
            check=True,
            capture_output=True,
        )
        out_size = tmp.stat().st_size
        # Verify the encode decodes before it overwrites the source. A
        # downsample has no re-download to fall back on, so a corrupt encode
        # must never replace a good original.
        if not _decode_ok(tmp):
            return (rel, sr, rate, None, "resampled file failed verification")
        os.replace(str(tmp), str(src))
        tmp = None
        return (rel, sr, rate, in_size - out_size, None)
    except subprocess.CalledProcessError as e:
        err = (e.stderr.decode(errors="replace")[:200] if e.stderr else "").strip()
        return (rel, sr, rate, None, err)
    except Exception as e:
        return (rel, sr, rate, None, str(e))
    finally:
        # Remove the leftover temp on any failure (ffmpeg error, interrupt,
        # exception). On success tmp is None and src is already swapped.
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def sweep_stale_encodes(directory):
    """Delete orphaned resample temps under `directory`, returning the count.

    A temp only survives the resample_one finally-cleanup if the process was
    hard-killed (OOM/power loss/SIGKILL) mid-encode. downsample_dir never runs
    concurrently against the same tree — the web path holds the staging lock,
    the CLI is single-threaded — so any temp present here is a dead orphan, not
    an encode in flight. Left alone they hide from the library scanner (dot
    prefix) but quietly accumulate disk.
    """
    removed = 0
    for p in Path(directory).rglob(_TMP_PREFIX + "*.flac"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def downsample_dir(directory, *, verbose=True, base_dir=None, log=print):
    """Resample any high-sample-rate FLACs inside `directory` (recursive).

    Probes each file's sample rate, resamples the ones above CD rate, and
    leaves the rest untouched. `base_dir` is the root paths are taken relative
    to (defaults to MUSIC_ROOT); pass the album/staging dir when the files
    aren't on the canonical music tree yet.

    Returns dict: {"resampled": int, "errors": int, "saved_bytes": int}.
    Never raises; on bad input returns zero counts.
    """
    directory = Path(directory)
    if not directory.exists() or not directory.is_dir():
        return {"resampled": 0, "errors": 0, "saved_bytes": 0}

    sweep_stale_encodes(directory)

    _bd = base_dir or MUSIC_ROOT
    af_filter, _ = detect_resampler_filter()

    candidates = []
    for p in directory.rglob("*.flac"):
        if p.name.startswith("."):
            continue
        try:
            rel = str(p.relative_to(_bd))
        except ValueError:
            # outside _bd — resample_one assumes _bd/rel
            continue
        sr = read_sample_rate(p)
        rate = target_rate(sr)
        if rate:
            candidates.append((rel, sr, rate))

    if not candidates:
        return {"resampled": 0, "errors": 0, "saved_bytes": 0}

    if verbose:
        log(f"  ⇳ downsample: {len(candidates)} file(s) in {directory.name}")

    saved_total = 0
    errors = 0
    resampled = 0

    with ThreadPoolExecutor(max_workers=RESAMPLE_WORKERS) as ex:
        futs = {ex.submit(resample_one, rel, sr, rate, af_filter, base_dir=_bd): rel
                for rel, sr, rate in candidates}
        for fut in as_completed(futs):
            rel, sr, rate, saved, err = fut.result()
            if err is not None:
                errors += 1
                if verbose:
                    log(f"  ✗ {Path(rel).name}: {err}")
            else:
                resampled += 1
                saved_total += saved

    if verbose and resampled:
        log(f"  ✓ downsample: {resampled} resampled, "
            f"saved {human(saved_total)}")

    return {"resampled": resampled,
            "errors": errors,
            "saved_bytes": saved_total}
