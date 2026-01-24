"""Streamrip download wrapper and staging utilities.

"""
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from qobuz_fetch import config as cfg
from qobuz_fetch.ui_cli.colors import C, fmt
from qobuz_fetch.ui_cli.logging import log, vlog

# Optional cancel-check hook. The web JobManager installs a callable
# here that returns True when the active job has been cancelled; rip_url
# polls it between proc.wait iterations and kills the subprocess group
# when it fires. None when running from the CLI (where Ctrl-C handles
# the same job via KeyboardInterrupt).
_CANCEL_CHECK = None


def set_cancel_check(fn):
    """Register a no-arg callable that returns True to cancel the active rip.

    Called by qobuz_fetch.web.jobs at import time.
    """
    global _CANCEL_CHECK
    _CANCEL_CHECK = fn

try:
    from mutagen.flac import FLAC as MutagenFLAC
    HAVE_MUTAGEN = True
except Exception:
    MutagenFLAC = None  # type: ignore
    HAVE_MUTAGEN = False

_FLAC_TRUNCATION_FLOOR = 150_000


# ── FLAC signature ────────────────────────────────────────────────────────────

def _flac_signature(path: Path):
    """Tag-tuple identifier for a FLAC, stable across beets's move.

    Lyric_fetch's state file is keyed by absolute path.
    Pre-import lyric runs leave state with
    staging-path keys that vanish once beets moves files to MUSIC_ROOT.
    Tag tuples survive that move because beets's `autotag: no` doesn't
    rewrite tags. We use these to find where a transient-from-staging
    file landed post-import, so the lyric retry manifest can hold the
    real (post-import) paths instead of stale staging paths.

    Returns None when mutagen is unavailable or the file can't be read
    (caller treats that as "can't track for retry"). All string fields
    are lowercased + stripped so trivial case differences don't break
    matching across re-runs."""
    if not HAVE_MUTAGEN:
        return None
    try:
        f = MutagenFLAC(str(path))
    except Exception:
        return None
    if f.tags is None:
        return None

    def _g(*keys):
        for k in keys:
            v = f.tags.get(k)
            if v and isinstance(v, list) and v[0]:
                return str(v[0]).strip()
        return ""

    from qobuz_fetch.library.scanner import parse_track_num
    try:
        disc = int(parse_track_num(_g("discnumber", "DISCNUMBER")) or 1) or 1
    except (TypeError, ValueError):
        disc = 1
    try:
        track = int(parse_track_num(_g("tracknumber", "TRACKNUMBER")) or 0)
    except (TypeError, ValueError):
        track = 0

    return (
        _g("albumartist", "ALBUMARTIST", "artist", "ARTIST").lower(),
        _g("album", "ALBUM").lower(),
        disc,
        track,
        _g("title", "TITLE").lower(),
    )


# ── FLAC validation ───────────────────────────────────────────────────────────

def is_flac(path: Path) -> bool:
    """Returns False when ffprobe identifies non-flac OR when the file is
    obviously broken (tiny size, zero duration). Trusts the .flac extension
    on ffprobe infrastructure failures.

    Also rejects truncated/empty files from interrupted streamrip
    downloads. These pass FLAC magic-bytes detection but contain no audio.
    Without this, partial downloads inflate n_ok, beets silently skips
    them, and the upgrade flow falsely declares success."""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    # 150KB floor — a 5-second FLAC at typical bitrates is well above this.
    # The lower 10KB floor let interrupted ~50KB downloads slip through when
    # ffprobe was unavailable (we trust the extension on ffprobe failure).
    if size < _FLAC_TRUNCATION_FLOOR:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {path.name} suspiciously small ({size}B); treating as broken."))
        return False
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format",
             "-select_streams", "a:0", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        log.info(fmt(C.YELLOW, f"  ⚠  ffprobe failed on {path.name}; trusting .flac extension."))
        return True
    if r.returncode != 0:
        log.info(fmt(C.YELLOW,
            f"  ⚠  ffprobe rejected {path.name} ({r.stderr.strip()[:100]}); treating as broken."))
        return False
    if not r.stdout:
        log.info(fmt(C.YELLOW, f"  ⚠  ffprobe gave no output for {path.name}; trusting extension."))
        return True
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        log.info(fmt(C.YELLOW, f"  ⚠  ffprobe output not JSON for {path.name}; trusting extension."))
        return True
    streams = data.get("streams", [])
    if not streams:
        return False
    if streams[0].get("codec_name", "").lower() != "flac":
        return False
    # Duration check catches files that have valid FLAC headers but no
    # actual audio samples (truncated mid-download).
    fmt_block = data.get("format") or {}
    try:
        duration = float(fmt_block.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration < 0.1:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {path.name} has no audio data ({duration:.1f}s); treating as broken."))
        return False
    return True


# ── Process helpers ───────────────────────────────────────────────────────────

def _kill_process_group(proc):
    """Best-effort: kill rip's whole process group, fall back to .kill().

    Guard against the start_new_session race: between Popen returning and
    the child running setsid(), the child's pgid is still *ours*. A
    killpg(SIGKILL) then would SIGKILL this process and the user's shell.
    Only group-kill once the child is verifiably in its own group;
    otherwise kill just the child pid.
    """
    try:
        pgid = os.getpgid(proc.pid)
        if pgid != os.getpgrp():
            os.killpg(pgid, signal.SIGKILL)
            return
    except OSError:
        pass  # child already gone, or getpgid failed → fall through
    try:
        proc.kill()  # child pid only — never our own group
    except OSError:
        pass


# ── streamrip wrapper ─────────────────────────────────────────────────────────

def rip_url(url, timeout=None, live_output=False, quality=None):
    """Run `rip url <url>`, returning (returncode, combined_output_string).

    live_output=True: stream rip's output to the terminal in real time via a
    reader thread — used for full-album downloads where silence for many minutes
    is unacceptable on mobile.

    start_new_session=True puts rip in its own process group so that on timeout
    OR Ctrl-C, os.killpg kills the entire tree (rip spawns its own downloader
    children; plain process.kill() only kills the parent, leaving them orphaned).
    """
    if timeout is None:
        timeout = cfg.RIP_TIMEOUT
    if quality is None:
        quality = cfg.STREAMRIP_QUALITY
    cmd = ["rip"]
    # streamrip does NOT honor a STREAMRIP_CONFIG env var; it looks at
    # ~/.config/streamrip/config.toml unless --config-path is passed. The
    # web Settings page writes credentials to cfg.STREAMRIP_CONFIG, so we
    # must point rip at that file explicitly or the saved creds are ignored.
    cmd += ["--config-path", str(cfg.STREAMRIP_CONFIG)]
    if quality is not None:
        cmd += ["-q", str(quality)]
    cmd += ["url", url]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, "rip: command not found"

    lines = []

    # Pre-compiled filters to strip Rich rules, timestamps, and
    # collapse each multi-line ERROR block to one concise line.
    _ts_re   = re.compile(r"^\s*\[\d{2}:\d{2}:\d{2}\]\s*")
    _rule_re = re.compile(r"^\s*[─━]{3,}")
    # Any line starting with a Unicode box-drawing char (U+2500–U+257F)
    # is part of a Rich panel/traceback. Streamrip prints a multi-line
    # traceback box on its `version_coro` post-processing bug; this filters
    # the noise out of live output. The line is still captured in `lines`
    # so error detection / auth-lost detection / rc-dump still work.
    _box_re  = re.compile(r"^[\u2500-\u257f]")
    # Rich's log handler appends "  module.py:LINE" columns at end of line —
    # anchor to EOL so a track title containing the literal "name.py:42"
    # isn't mangled.
    _src_re  = re.compile(r"\s+\w+\.py:\d+\s*$")
    # Rich also prefixes records with the log level name ("INFO     …").
    # Drop DEBUG/INFO/WARNING/CRITICAL but leave ERROR intact so the
    # existing multi-line error-grouping logic can detect it. \s* tolerates
    # Rich's left-padding on narrow terminals.
    _lvl_re  = re.compile(r"^\s*(?:DEBUG|INFO|WARNING|CRITICAL)\s+")

    def _reader():
        banner_lines_remaining = 0
        in_error = False
        err_buf = []

        def emit_error():
            nonlocal in_error, err_buf
            if err_buf:
                text = " ".join(s.strip() for s in err_buf if s.strip())
                text = re.sub(r"\s+", " ", text)
                m = re.search(r"Persistent error downloading track '([^']+)', skipping", text)
                if m:
                    log.info(fmt(C.RED, f"    ✗ skipped: {m.group(1)} (network)"))
                else:
                    m = re.search(r"Error downloading track '([^']+)', retrying", text)
                    if m:
                        log.info(fmt(C.YELLOW, f"    ⟳ retry: {m.group(1)}"))
                    else:
                        short = text[:100] + ("…" if len(text) > 100 else "")
                        log.info(fmt(C.RED, "    ✗ " + short))
            in_error = False
            err_buf = []

        for line in proc.stdout:
            lines.append(line)
            if not live_output:
                continue
            stripped = line.strip()
            if banner_lines_remaining == 0 and (
                "new version of streamrip" in stripped.lower()
                or "pip install streamrip" in stripped.lower()
                or "pip3 install streamrip" in stripped.lower()
            ):
                banner_lines_remaining = 40
                continue
            if banner_lines_remaining > 0:
                banner_lines_remaining -= 1
                continue
            if not stripped or _rule_re.match(stripped) or _box_re.match(stripped):
                if in_error: emit_error()
                continue
            cleaned = _ts_re.sub("", line.rstrip())
            if "ERROR" in cleaned[:40]:
                if in_error: emit_error()
                in_error = True
                err_buf = [cleaned]
                continue
            if in_error:
                if line.startswith("    "):
                    err_buf.append(cleaned)
                    continue
                emit_error()
            log.info("    " + _src_re.sub("", _lvl_re.sub("", cleaned)))
        emit_error()

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout if timeout else None
    try:
        while True:
            if _CANCEL_CHECK is not None and _CANCEL_CHECK():
                _kill_process_group(proc)
                reader.join(timeout=5)
                return 130, "".join(lines) + "\n<<< canceled by user >>>"
            poll_for = 1.0
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                poll_for = min(poll_for, remaining)
            try:
                proc.wait(timeout=poll_for)
                break
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired:
        # Kill the whole process group (covers rip's child downloaders too).
        _kill_process_group(proc)
        reader.join(timeout=5)
        return 1, "".join(lines) + (
            f"\n<<< rip timed out after {timeout}s — try a smaller batch "
            "or raise RIP_TIMEOUT in compose.yaml. >>>")
    except KeyboardInterrupt:
        # Ctrl-C: with start_new_session=True the SIGINT did NOT propagate to
        # rip's group. Without explicit killpg, rip + its downloader children
        # keep running after this script exits — wasted bandwidth, zombie files.
        _kill_process_group(proc)
        reader.join(timeout=5)
        raise

    reader.join(timeout=5)
    return proc.returncode, "".join(lines)


# ── Staging snapshot / cleanup ────────────────────────────────────────────────

def snapshot_staging():
    if not cfg.STAGING_DIR.exists():
        return set()
    return {p for p in cfg.STAGING_DIR.rglob("*") if p.is_file()}


def files_added_since(prior_snapshot):
    return [p for p in cfg.STAGING_DIR.rglob("*") if p not in prior_snapshot and p.is_file()]


def cleanup_lossy(new_files):
    """Keep FLACs, delete anything else. Confirms FLAC codec via ffprobe — Qobuz
    silently downgrades to lossy when hi-res isn't available for the user's tier."""
    kept, deleted = [], []
    for f in new_files:
        ext = f.suffix.lower()
        if ext == ".flac":
            if is_flac(f):
                kept.append(f)
            else:
                try:
                    f.unlink()
                    deleted.append(f.stem)
                except OSError as e:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Couldn't remove broken FLAC {f.name}: {e}."))
        elif ext in cfg.AUDIO_EXTS:
            try:
                f.unlink()
                deleted.append(f.stem)
            except OSError as e:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Couldn't remove non-FLAC {f.name}: {e}."))
    return kept, deleted


# Known non-audio artifacts that streamrip leaves in STAGING_DIR between runs.
# These inflate the leftover count and can trigger the --yes abort threshold.
_RESIDUE_EXTS  = {".jpg", ".jpeg", ".png", ".gif", ".json", ".log", ".toml"}
_RESIDUE_NAMES = {"cover", "albumartwork", "artwork"}


def _dir_is_all_residue(d):
    """True if every file under d has a residue extension (or d is empty).

    A .cue, .nfo, or any non-residue file makes this return False, keeping the
    directory intact even if it has a residue-like name.
    """
    try:
        for child in d.rglob("*"):
            if child.is_file() and child.suffix.lower() not in _RESIDUE_EXTS:
                return False
    except OSError:
        return False
    return True


def cleanup_staging_residue():
    """Remove known streamrip non-audio residue from STAGING_DIR.

    Streamrip can leave cover art (cover.jpg), JSON metadata, and log files
    behind after failed or interrupted downloads. These accumulate across runs
    and bloat the staging leftover count, triggering --yes to abort at
    LEFTOVER_WARN_LIMIT even when there are no actual leftover audio files.

    A residue-named directory (cover/, artwork/, albumartwork/) is only removed
    when every file inside it has a residue extension. A .cue sheet, .nfo, or
    any audio file saves it — it's a real album that happens to share a name
    with a common streamrip artefact.

    Returns the number of items removed.
    """
    if not cfg.STAGING_DIR.exists():
        return 0
    removed = 0
    # Walk dirs bottom-up so we can rmdir empty residue dirs after unlinking files.
    for p in sorted(cfg.STAGING_DIR.rglob("*"), key=lambda x: -len(x.parts)):
        try:
            if p.is_file() and p.suffix.lower() in _RESIDUE_EXTS:
                p.unlink()
                removed += 1
                vlog(f"residue removed: {p.relative_to(cfg.STAGING_DIR)}")
            elif p.is_dir() and p.name.lower() in _RESIDUE_NAMES:
                if not _dir_is_all_residue(p):
                    vlog(f"residue dir kept (contains non-residue files): "
                         f"{p.relative_to(cfg.STAGING_DIR)}")
                    continue
                shutil.rmtree(p)
                removed += 1
                vlog(f"residue dir removed: {p.relative_to(cfg.STAGING_DIR)}")
        except OSError:
            pass
    return removed
