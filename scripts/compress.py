#!/usr/bin/env python3
"""compress: find high-sample-rate FLACs and resample them down.

Targets (preserves integer-ratio family for clean math):
  88.2 / 176.4 / 352.8 kHz  →  44.1 kHz
  96   / 192   / 384   kHz  →  48   kHz

Quality settings:
  - Resampler: soxr at precision 28 if available, else swresample with
    filter_size=512, phase_shift=20 (very high quality).
  - Triangular high-pass dither (decorrelates quantization noise).
  - Source bit depth preserved (16-bit stays 16-bit, 24-bit stays 24-bit).
  - FLAC compression level 5 (default — lossless, fast encode).

Usage:
    compress                       # live run
    DRY_RUN=1 compress             # show what would happen
    RESAMPLE_WORKERS=6 compress    # crank parallelism (default 4)
    PROBE_WORKERS=24 compress      # tune probe parallelism (default 16)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MUSIC_ROOT       = Path(os.environ.get("MUSIC_ROOT", "/music"))


def _default_cache_dir() -> Path:
    # Inside the container the app sets DATA_DIR=/data (a persistent volume),
    # so cache survives restarts. Outside the container, fall back to the
    # standard XDG cache location.
    data = os.environ.get("DATA_DIR")
    if data:
        return Path(data)
    xdg = os.environ.get("XDG_CACHE_HOME")
    return Path(xdg) / "qobuz-librarian" if xdg else Path.home() / ".cache" / "qobuz-librarian"


CACHE_FILE       = Path(os.environ.get("COMPRESS_CACHE_FILE", "")) \
                   or (_default_cache_dir() / "compress_cache.json")
# 1024 is enough to clear large ID3v2 preambles on the rare track that has one.
PROBE_BYTES      = 1024
PROBE_WORKERS    = int(os.environ.get("PROBE_WORKERS", "16"))
RESAMPLE_WORKERS = int(os.environ.get("RESAMPLE_WORKERS", "4"))
DRY_RUN          = os.environ.get("DRY_RUN") == "1"

try:
    CACHE = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
except (OSError, ValueError):
    CACHE = {}


def save_cache():
    # Read on-disk first and merge our entries on top, so concurrent
    # processes don't overwrite each other's new entries.
    try:
        on_disk = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    except (OSError, ValueError):
        on_disk = {}
    on_disk.update(CACHE)
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(on_disk))
    os.replace(tmp, CACHE_FILE)


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


def probe(rel: str):
    """Read a library file's sample rate, returning (rel, sample_rate)."""
    return rel, read_sample_rate(MUSIC_ROOT / rel)


def discover():
    """Walk MUSIC_ROOT, return rel paths of all FLACs."""
    out = []
    for root, dirs, files in os.walk(MUSIC_ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".flac") and not f.startswith("."):
                out.append(str((Path(root) / f).relative_to(MUSIC_ROOT)))
    return out


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
    compress_dir runs the same probe before it resamples.
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
    hard-killed (OOM/power loss/SIGKILL) mid-encode. compress_dir never runs
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


def compress_dir(directory, *, verbose=True, base_dir=None, log=print):
    """Resample any high-sample-rate FLACs inside `directory` (recursive).

    For per-album / post-import calls. Skips files already
    at target rates. Updates the global cache so a later full-library
    run won't re-probe these files.

    Returns dict: {"resampled": int, "errors": int, "saved_bytes": int}.
    Never raises; on bad input returns zero counts.
    """
    directory = Path(directory)
    if not directory.exists() or not directory.is_dir():
        return {"resampled": 0, "errors": 0, "saved_bytes": 0}

    sweep_stale_encodes(directory)

    # When base_dir is supplied (e.g. STAGING_DIR),
    # operate on paths relative to that root and disable the on-disk
    # cache. Cache keys are MUSIC_ROOT-relative; staging paths would
    # poison them with entries that vanish when beets imports.
    _bd = base_dir or MUSIC_ROOT
    _use_cache = base_dir is None

    af_filter, _ = detect_resampler_filter()

    flacs = [p for p in directory.rglob("*.flac") if not p.name.startswith(".")]
    if not flacs:
        return {"resampled": 0, "errors": 0, "saved_bytes": 0}

    candidates = []
    for p in flacs:
        try:
            rel = str(p.relative_to(_bd))
        except ValueError:
            # outside _bd — resample_one assumes _bd/rel
            continue
        # Cache hit: trust the previously-probed sample rate. The cache is
        # only populated from successful probes / resamples (resample_one
        # writes the target rate post-resample) so a hit means we already
        # know whether this file needs work.
        cached_sr = CACHE.get(rel) if _use_cache else None
        if cached_sr is not None:
            sr = int(cached_sr)
            rate = target_rate(sr)
            if not rate:
                continue
            candidates.append((rel, sr, rate))
            continue
        sr = read_sample_rate(p)
        # Record what we just learned so future calls don't re-probe.
        if sr > 0 and _use_cache:
            CACHE[rel] = sr
        rate = target_rate(sr)
        if not rate:
            continue
        candidates.append((rel, sr, rate))

    if not candidates:
        if _use_cache:
            save_cache()
        return {"resampled": 0, "errors": 0, "saved_bytes": 0}

    if verbose:
        log(f"  ⇳ compress: {len(candidates)} file(s) in {directory.name}")

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
                if _use_cache:
                    CACHE[rel] = rate

    if _use_cache and (resampled or errors):
        save_cache()

    if verbose and resampled:
        log(f"  ✓ compress: {resampled} resampled, "
            f"saved {human(saved_total)}")

    return {"resampled": resampled,
            "errors": errors,
            "saved_bytes": saved_total}


def main():
    if not MUSIC_ROOT.exists():
        print(f"ERROR: {MUSIC_ROOT} does not exist")
        sys.exit(1)

    af_filter, resampler_name = detect_resampler_filter()

    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print(f"\n{'=' * 60}\n  compress  |  {mode}  |  resampler: {resampler_name}\n{'=' * 60}")

    if not DRY_RUN:
        swept = sweep_stale_encodes(MUSIC_ROOT)
        if swept:
            print(f"  cleared {swept} stale temp file(s) from a prior interrupted run")

    print("  scanning library (listing FLACs)...")
    t0 = time.monotonic()
    files = discover()
    print(f"  found {len(files)} FLACs in {time.monotonic() - t0:.1f}s")

    todo = [f for f in files if f not in CACHE]
    print(f"  {len(files) - len(todo)} cached, {len(todo)} to probe\n")

    if todo:
        print(f"  probing headers ({PROBE_WORKERS} workers, {PROBE_BYTES} bytes each)...")
        t0 = time.monotonic()
        done = 0
        try:
            with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
                futs = {ex.submit(probe, f): f for f in todo}
                for fut in as_completed(futs):
                    rel, sr = fut.result()
                    if sr > 0:
                        CACHE[rel] = sr
                    done += 1
                    if done % 200 == 0 or done == len(todo):
                        el = time.monotonic() - t0
                        rps = done / el if el else 0
                        eta = (len(todo) - done) / rps if rps else 0
                        print(f"    {done}/{len(todo)}  ({rps:.1f}/s, ETA {eta:.0f}s)")
                        save_cache()
        except KeyboardInterrupt:
            save_cache()
            print("\n  interrupted, cache saved")
            sys.exit(130)
        save_cache()
        print(f"  probe done in {time.monotonic() - t0:.1f}s\n")

    candidates = []
    for rel in files:
        sr = CACHE.get(rel, 0)
        rate = target_rate(sr)
        if rate:
            candidates.append((rel, sr, rate))

    print(f"  {len(candidates)} eligible for resampling")

    if not candidates:
        print("  nothing to do.\n")
        return

    if DRY_RUN:
        for rel, sr, rate in candidates[:30]:
            print(f"  would: {Path(rel).name}  ({sr // 1000}→{rate // 1000}kHz)")
        if len(candidates) > 30:
            print(f"  ... and {len(candidates) - 30} more")
        print()
        return

    print(f"\n  resampling with {RESAMPLE_WORKERS} parallel workers ({resampler_name})...\n")
    t_start = time.monotonic()
    saved_total = 0
    errors = 0
    done = 0

    try:
        with ThreadPoolExecutor(max_workers=RESAMPLE_WORKERS) as ex:
            futs = {
                ex.submit(resample_one, rel, sr, rate, af_filter): rel
                for rel, sr, rate in candidates
            }
            for fut in as_completed(futs):
                rel, sr, rate, saved, err = fut.result()
                done += 1
                short = Path(rel).name
                if err is not None:
                    errors += 1
                    print(f"[{done}/{len(candidates)}] ERROR {short}: {err}")
                else:
                    saved_total += saved
                    CACHE[rel] = rate
                    elapsed = time.monotonic() - t_start
                    rate_per_min = (done / elapsed * 60) if elapsed else 0
                    eta_min = (len(candidates) - done) / rate_per_min if rate_per_min else 0
                    print(
                        f"[{done}/{len(candidates)}] {short}  "
                        f"({sr // 1000}→{rate // 1000}kHz)  "
                        f"saved {human(saved)}  total {human(saved_total)}  "
                        f"({rate_per_min:.1f}/min, ETA {eta_min:.0f}m)"
                    )
                if done % 10 == 0:
                    save_cache()
    except KeyboardInterrupt:
        save_cache()
        print("\n  interrupted, cache saved (in-flight workers will finish)")
        sys.exit(130)

    save_cache()
    print(f"\n{'=' * 60}")
    print(f"  done  |  resampled: {done - errors}  |  errors: {errors}")
    print(f"  total saved: {human(saved_total)}")
    print(f"  elapsed: {(time.monotonic() - t_start) / 60:.1f} minutes")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        save_cache()
        sys.exit(130)
