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
import stat
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from qobuz_librarian.integrations.rip import flac_audio_offset

# Downsampling needs ffmpeg to resample AND flac to verify the result. The
# overwrite is in-place and irreversible, so without the verifier there's no way
# to confirm a good encode replaced the only hi-res copy — treat the feature as
# unavailable rather than overwrite unverified. Both are pinned in the image, so
# this is always True there; a bare checkout missing either skips the step.
HAVE_DOWNSAMPLE = (shutil.which("ffmpeg") is not None
                   and shutil.which("flac") is not None)

MUSIC_ROOT       = Path(os.environ.get("MUSIC_ROOT", "/music"))
# 1024 is enough to clear large ID3v2 preambles on the rare track that has one.
PROBE_BYTES      = 1024
try:
    RESAMPLE_WORKERS = max(1, int(os.environ.get("RESAMPLE_WORKERS", "4")))
except ValueError:
    RESAMPLE_WORKERS = 4  # a bad override must not poison import or crash the pool


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
    """Bit depth of a local FLAC. The 1 KB header byte-parse settles the common
    case in a single read; metaflac backstops a file whose STREAMINFO sits past
    the probe window (a leading ID3 tag would do it) so the resample never
    upconverts a 24-bit master to s32 off a misread. Returns 0 on failure.

    Mirrors read_sample_rate: a downsample run reads this per selected file, so
    the cheap byte read leads and the metaflac subprocess is the exception. A
    byte-parse that can't find the header returns 0 and defers to metaflac, so
    leading the cheap path costs no reliability."""
    try:
        with open(path, "rb") as f:
            data = f.read(PROBE_BYTES)
        _, bps = parse_flac_info(data)
    except OSError:
        return 0
    if bps:
        return bps
    try:
        out = subprocess.run(
            ["metaflac", "--show-bps", str(path)],
            capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out) if out else 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, ValueError):
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


def parse_flac_total_samples(data: bytes) -> int:
    """Return the 36-bit total_samples from FLAC STREAMINFO bytes, or 0.

    FLAC stores 0 for 'unknown/streaming', which this also returns as 0 so
    callers treat it as 'don't know' rather than 'zero-length'."""
    i = data.find(b"fLaC")
    if i < 0:
        return 0
    si = i + 8  # skip fLaC(4) + metadata block header(4)
    if len(data) < si + 18:
        return 0
    return (((data[si + 13] & 0x0F) << 32)
            | (data[si + 14] << 24)
            | (data[si + 15] << 16)
            | (data[si + 16] << 8)
            | data[si + 17])


def read_total_samples(path: Path) -> int:
    """Total decoded sample count of a local FLAC, or 0 if unknown/unreadable.

    Same shape as read_sample_rate: the 1 KB header byte-parse settles the
    common case, and metaflac backstops a file whose STREAMINFO sits past the
    probe window (a leading ID3 tag). Used to confirm a resample didn't drop
    audio off a truncated source."""
    try:
        with open(path, "rb") as f:
            data = f.read(PROBE_BYTES)
        n = parse_flac_total_samples(data)
    except OSError:
        return 0
    if n:
        return n
    try:
        out = subprocess.run(
            ["metaflac", "--show-total-samples", str(path)],
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
    for p in directory.rglob("*"):
        # Case-insensitive: the rest of the app indexes *.FLAC too (suffix.lower),
        # so a case-sensitive glob would hide uppercase-extension files here.
        if (p.name.startswith(".") or not p.is_file()
                or p.suffix.lower() != ".flac"):
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
        offset = flac_audio_offset(p)
        audio_size = max(0, size - offset) if offset else size
        hires.append({"path": str(p), "sr": sr, "target": rate,
                      "size": size, "audio_size": audio_size})
    return {"hires": hires, "n_flac": n_flac}


def _decode_ok(path):
    """True only if the FLAC at `path` decodes cleanly (flac -t, frame-CRC +
    decode).

    The downsample overwrites the source in place with no re-download to fall
    back on, so anything that leaves the encode unverified — a decode failure, a
    timeout, or a missing/unusable flac binary (HAVE_DOWNSAMPLE already requires
    one, but it could vanish mid-run on a network mount) — returns False, and the
    encode is discarded rather than allowed to replace the original."""
    try:
        r = subprocess.run(["flac", "-t", "-s", str(path)],
                           capture_output=True, timeout=300,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


# Resample output is written to a dot-prefixed temp beside the source, then
# atomically swapped in. The dot keeps the library scanner from indexing it
# mid-encode; the shared prefix lets a later run recognise and sweep one an
# interrupt left behind.
_TMP_PREFIX = ".compress-"


def _encode_opts_for_bps(bps, af_filter):
    """ffmpeg options that keep the output FLAC at the source bit depth.

    Returns (af, sample_fmt, depth_args). The encoder, not the s16/s32 sample
    format alone, sets the FLAC bit depth: a 24-bit source fed as s32 is written
    as a 32-bit stream unless -bits_per_raw_sample 24 says otherwise, which would
    inflate the master (larger file, wrong reported depth). 16-bit uses s16; any
    other known depth (8/24/32) is pinned via -bits_per_raw_sample so the output
    keeps the source depth instead of being silently padded. An unknown depth
    (0, unreadable) keeps the conservative s32 default with no pin.
    """
    if bps == 16:
        return af_filter, "s16", []
    if bps in (8, 24, 32):
        return (f"{af_filter},aformat=sample_fmts=s32", "s32",
                ["-bits_per_raw_sample", str(bps)])
    return af_filter, "s32", []


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
        st = src.stat()
        in_size = st.st_size
        src_mode = stat.S_IMODE(st.st_mode)

        bps = read_local_bit_depth(src)
        af, sample_fmt, depth_args = _encode_opts_for_bps(bps, af_filter)

        fd, tmp_name = tempfile.mkstemp(
            dir=str(src.parent), prefix=_TMP_PREFIX, suffix=".flac")
        os.close(fd)
        tmp = Path(tmp_name)

        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(src),
                # Map every input stream so all embedded PICTURE blocks
                # (front+back cover) survive — ffmpeg's default selection keeps
                # only one video stream and would drop the rest.
                "-map", "0",
                "-af", af,
                "-ar", str(rate),
                "-sample_fmt", sample_fmt,
                *depth_args,
                "-c:a", "flac",
                # Copy attached art bit-for-bit. Without this the FLAC muxer's
                # default video codec (PNG) transcodes the embedded JPEG cover,
                # inflating the file (a multi-MB Qobuz cover can wipe out the
                # audio savings and make the output net-larger).
                "-c:v", "copy",
                "-map_metadata", "0",
                "-y", str(tmp),
            ],
            check=True,
            capture_output=True,
            # A hung NFS/FUSE mount must not freeze the worker forever; cap the
            # encode. Generous even for a long 24/192 file on slow hardware.
            timeout=600,
        )
        out_size = tmp.stat().st_size
        # Verify the encode decodes before it overwrites the source. A
        # downsample has no re-download to fall back on, so a corrupt encode
        # must never replace a good original.
        if not _decode_ok(tmp):
            return (rel, sr, rate, None, "resampled file failed verification")
        # Never let a resample silently change the bit depth — a 24-bit master
        # must not come back 32-bit, nor an 8-bit file get padded to 24. Checked
        # for any known source depth; an unknown depth (bps == 0, unreadable)
        # can't be verified and falls through. Original untouched on a mismatch.
        if bps:
            out_bps = read_local_bit_depth(tmp)
            if out_bps != bps:
                return (rel, sr, rate, None,
                        f"resampled to {out_bps}-bit, expected {bps}-bit; "
                        "left the original untouched")
        # Verify the resample kept the whole stream. ffmpeg exits 0 on a
        # truncated or mid-file-corrupt source, emitting only the decodable
        # prefix, and flac -t then passes on that short output — so a damaged
        # hi-res master (the exact file class the repair feature exists to
        # catch) could be silently replaced by a shortened encode, laundering
        # the damage out of every later integrity probe. Compare the output's
        # sample count to the source scaled by the rate ratio.
        in_samples = read_total_samples(src)
        out_samples = read_total_samples(tmp)
        if not (in_samples and out_samples):
            # A 0 = STREAMINFO 'unknown' on either side means the length can't be
            # verified. Previously this SKIPPED the check and replaced the master
            # anyway, so a truncated-but-decodable encode of an unknown-length
            # source slipped through. Refuse instead — keep the original; an
            # un-shrunk file is a far cheaper outcome than a silently-truncated
            # master with no recovery path.
            return (rel, sr, rate, None,
                    "couldn't verify resampled length (source/output STREAMINFO "
                    "reports unknown sample count); left the original untouched")
        expected = in_samples * rate / sr
        # Cap the relative term at ~1s of output. expected*0.005 alone scales
        # with length (~21s on a 70-min master), which would let a long source
        # lose many seconds and still pass — the opposite of this gate's job.
        tol = max(rate // 10, min(expected * 0.005, rate))
        if abs(out_samples - expected) > tol:
            return (rel, sr, rate, None,
                    f"resampled to {out_samples / rate:.1f}s, expected "
                    f"~{in_samples / sr:.1f}s; left the original untouched "
                    "(source may be truncated)")
        # With art copied bit-for-bit a real downsample always shrinks the file;
        # if it somehow didn't, replacing the source only churns it (and a
        # larger output would drive saved_bytes negative), so keep the original.
        if out_size >= in_size:
            return (rel, sr, rate, None,
                    "resampled output not smaller than source; "
                    "left the original untouched")
        # tempfile.mkstemp makes the temp 0o600; carry the source's mode across
        # the swap so a downsample doesn't quietly tighten a 0o644 library file
        # to owner-only (an annoyance on shared/NAS libraries).
        try:
            os.chmod(str(tmp), src_mode)
        except OSError:
            pass
        os.replace(str(tmp), str(src))
        tmp = None
        return (rel, sr, rate, in_size - out_size, None)
    except subprocess.TimeoutExpired:
        return (rel, sr, rate, None,
                "ffmpeg timed out (slow storage?); left the original untouched")
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
    for p in directory.rglob("*"):
        if p.name.startswith(".") or not p.is_file() or p.suffix.lower() != ".flac":
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
        try:
            for fut in as_completed(futs):
                rel, sr, rate, saved, err = fut.result()
                if err is not None:
                    errors += 1
                    if verbose:
                        log(f"  ✗ {Path(rel).name}: {err}")
                else:
                    resampled += 1
                    saved_total += saved
        except KeyboardInterrupt:
            # Stop promptly: discard not-yet-started encodes instead of letting
            # the context manager block on shutdown(wait=True) for the whole queue.
            ex.shutdown(wait=False, cancel_futures=True)
            raise

    if verbose and resampled:
        log(f"  ✓ downsample: {resampled} resampled, "
            f"saved {human(saved_total)}")

    return {"resampled": resampled,
            "errors": errors,
            "saved_bytes": saved_total}
