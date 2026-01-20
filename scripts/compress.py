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


def detect_resampler_filter():
    """Return the -af filter string to use, based on what ffmpeg has compiled in."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-resamplers"],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0 and b"soxr" in r.stdout:
            return ("aresample=resampler=soxr:precision=28:dither_method=triangular_hp", "soxr p28")
    except Exception:
        pass
    return ("aresample=filter_size=512:phase_shift=20:dither_method=triangular_hp", "swresample HQ")


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
    """Read bit depth from a local FLAC file header. Returns 0 on failure."""
    try:
        with open(path, "rb") as f:
            data = f.read(64)
        _, bps = parse_flac_info(data)
        return bps
    except OSError:
        return 0


def probe(rel: str):
    """Read first PROBE_BYTES from MUSIC_ROOT, return (rel, sample_rate)."""
    try:
        with open(MUSIC_ROOT / rel, "rb") as f:
            data = f.read(PROBE_BYTES)
        sr, _ = parse_flac_info(data)
        return rel, sr
    except OSError:
        return rel, 0


def discover():
    """Walk MUSIC_ROOT, return rel paths of all FLACs."""
    out = []
    for root, dirs, files in os.walk(MUSIC_ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".flac"):
                out.append(str((Path(root) / f).relative_to(MUSIC_ROOT)))
    return out


def target_rate(sr):
    if sr in (88200, 176400, 352800):
        return 44100
    if sr in (96000, 192000, 384000):
        return 48000
    return None


def resample_one(rel, sr, rate, af_filter, *, base_dir=None):
    """Resample one file. Returns (rel, sr, rate, saved_bytes, error_msg).

    base_dir defaults to MUSIC_ROOT. Pass an alternate root (e.g.
    STAGING_DIR) when operating on files that have not been imported
    onto the canonical music tree yet.
    """
    src = (base_dir or MUSIC_ROOT) / rel
    # Encode to a temp file in the SAME directory as the source, then swap
    # it in with os.replace(). os.replace() is an atomic same-filesystem
    # rename, so an interrupt (Ctrl+C / OOM / power loss) can never leave a
    # half-written file where the original was: either the old file or the
    # fully-encoded new one is present, nothing in between. A previous
    # implementation encoded into a scratch dir (often a different
    # filesystem from the music tree) and moved the result over the source —
    # cross-device that degrades to copy-then-unlink, and an interrupt
    # mid-copy destroyed the user's only lossless copy. The temp name is
    # dot-prefixed so a library scanner (which skips dotfiles) never indexes
    # the transient file.
    tmp = None
    try:
        in_size = src.stat().st_size

        bps = read_local_bit_depth(src)
        sample_fmt = "s16" if bps == 16 else "s32"

        fd, tmp_name = tempfile.mkstemp(
            dir=str(src.parent), prefix=".compress-", suffix=".flac")
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

    # When base_dir is supplied (e.g. STAGING_DIR),
    # operate on paths relative to that root and disable the on-disk
    # cache. Cache keys are MUSIC_ROOT-relative; staging paths would
    # poison them with entries that vanish when beets imports.
    _bd = base_dir or MUSIC_ROOT
    _use_cache = base_dir is None

    af_filter, _ = detect_resampler_filter()

    flacs = list(directory.rglob("*.flac"))
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
        try:
            with open(p, "rb") as f:
                data = f.read(PROBE_BYTES)
            sr, _ = parse_flac_info(data)
        except OSError:
            continue
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
