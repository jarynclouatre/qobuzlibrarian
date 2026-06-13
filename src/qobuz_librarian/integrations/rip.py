"""Streamrip download wrapper and staging utilities."""
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log, vlog, wrap_thread_target

# Optional cancel-check hook. The web JobManager installs a callable
# here that returns True when the active job has been cancelled; rip_url
# polls it between proc.wait iterations and kills the subprocess group
# when it fires. None when running from the CLI (where Ctrl-C handles
# the same job via KeyboardInterrupt).
_CANCEL_CHECK = None


def set_cancel_check(fn):
    """Register a no-arg callable that returns True to cancel the active rip.

    Called by qobuz_librarian.web.jobs at import time.
    """
    global _CANCEL_CHECK
    _CANCEL_CHECK = fn


def is_cancel_requested():
    """True when the active web job has been cancelled. Always False on the
    CLI path (no hook installed; Ctrl-C raises KeyboardInterrupt instead).
    Lets post-rip code skip the beets import after a cancel."""
    return bool(_CANCEL_CHECK and _CANCEL_CHECK())

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

    from qobuz_librarian.library.scanner import parse_track_num
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

def flac_audio_ok(path):
    """Verify a FLAC's audio with ``flac -t`` (decode + per-frame CRC check).

    Returns True/False, or None when the flac tool isn't installed so the
    caller can choose its own fallback. ``flac -t`` reads only the audio
    stream — the embedded cover art streamrip always writes (sometimes the
    oversized "original" Qobuz art) never enters the decode, so a track with
    intact audio but a malformed picture isn't mistaken for a broken download.
    The timeout caps a pathological hang without tripping on long tracks (FLAC
    verifies far faster than real time)."""
    if shutil.which("flac") is None:
        return None
    try:
        proc = subprocess.run(
            ["flac", "-t", "-s", str(path)],
            capture_output=True, timeout=300, stdin=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return proc.returncode == 0


def flac_audio_offset(path):
    """Byte offset of the first audio frame — the ``fLaC`` marker plus every
    metadata block. Lets a caller weigh the audio stream apart from metadata and
    embedded art, which don't shrink on resample and survive the tail damage that
    truncates the audio. Returns 0 when the file isn't a plain FLAC or the header
    is unreadable, so callers fall back to a whole-file size, not a wrong answer."""
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


def is_flac(path: Path) -> bool:
    """True when the file is a complete, decodable FLAC.

    An interrupted streamrip download leaves a truncated file behind whose
    header still advertises the full duration, so a metadata probe reports
    the whole track length even though most audio frames are missing — only
    verifying the frames exposes the gap. Without catching it, partials
    inflate n_ok, beets silently skips them on import, and the upgrade flow
    declares a false success.

    ``flac -t`` does that verification (every frame's CRC). When the flac tool
    isn't installed we fall back to a size heuristic: a few seconds of FLAC
    sits well above the floor, so anything smaller is almost certainly a
    partial; larger files we can't verify, so we trust them."""
    try:
        if path.stat().st_size == 0:
            return False
    except OSError:
        return False

    ok = flac_audio_ok(path)
    if ok is not None:
        if not ok:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {path.name} won't decode cleanly (truncated/corrupt); "
                "treating as broken."))
        return ok

    try:
        small = path.stat().st_size < _FLAC_TRUNCATION_FLOOR
    except OSError:
        small = True
    if small:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {path.name} suspiciously small and flac tool unavailable; "
            "treating as broken."))
        return False
    log.info(fmt(C.YELLOW,
        f"  ⚠  flac tool unavailable for {path.name}; trusting .flac extension."))
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


def _terminate(proc, reader):
    """Kill the rip process group, reap the child, and drain the reader.

    Reaping matters: in the container uvicorn runs as PID 1 with no init to
    harvest orphans, so a killed rip left unwaited stays a zombie for the
    life of the web process, one per cancel or timeout. Once the child is
    reaped its stdout is closed, so joining the reader afterwards can't race
    the ``lines`` buffer the caller is about to read.
    """
    _kill_process_group(proc)
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    reader.join(timeout=5)


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
    # The librarian does its own missing-track bookkeeping (it re-rips for
    # quality upgrades, truncation repair, and broken-FLAC retries), so
    # streamrip's downloads database — which silently skips any URL it has
    # already logged, exiting 0 with no new file — must be off. The Docker
    # entrypoint forces downloads_enabled=false in the config, but a
    # bare-metal/CLI run can fall back to the user's own
    # ~/.config/streamrip/config.toml where it defaults ON; --no-db (a global
    # rip flag) makes every re-rip work regardless of the config.
    cmd += ["--no-db"]
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

    # Wrap so the reader thread inherits the spawning job's context — its
    # streamrip progress + per-track error lines log via the shared logger and
    # would otherwise be dropped by the job-log handler's thread filter.
    reader = threading.Thread(target=wrap_thread_target(_reader), daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout if timeout else None
    try:
        while True:
            if _CANCEL_CHECK is not None and _CANCEL_CHECK():
                _terminate(proc, reader)
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
        _terminate(proc, reader)
        # 124 (the conventional timeout exit code) so a timeout is
        # distinguishable from a generic rip failure (rc=1) in the logs.
        return 124, "".join(lines) + (
            f"\n<<< rip timed out after {timeout}s — try a smaller batch "
            "or raise RIP_TIMEOUT in compose.yaml. >>>")
    except KeyboardInterrupt:
        # Ctrl-C: with start_new_session=True the SIGINT did NOT propagate to
        # rip's group, so rip + its children would keep running after this
        # process exits — wasted bandwidth, half-written staging files.
        _terminate(proc, reader)
        raise

    reader.join(timeout=5)
    return proc.returncode, "".join(lines)


# ── Staging snapshot / cleanup ────────────────────────────────────────────────

def _iter_staging_files():
    """Yield every staging file, skipping the retry-park tree entirely.

    .beets_retry/ holds re-import-queued album dirs that accumulate over a
    long-running session; walking them on every snapshot was the dominant
    per-album cost on big queue flushes, when nothing about those files
    matters here. Skipping the tree (rather than rglob-then-filter) keeps
    the walk proportional to real staging activity, not the parked backlog.
    """
    if not cfg.STAGING_DIR.exists():
        return
    retry_name = cfg.BEETS_RETRY_DIR
    for entry in cfg.STAGING_DIR.iterdir():
        if entry.name == retry_name:
            continue
        if entry.is_file():
            yield entry
        elif entry.is_dir():
            yield from (p for p in entry.rglob("*") if p.is_file())


def snapshot_staging():
    return set(_iter_staging_files())


def files_added_since(prior_snapshot):
    return [p for p in _iter_staging_files() if p not in prior_snapshot]


def cleanup_lossy(new_files):
    """Sort freshly-downloaded audio into (kept, lossy, broken), deleting both
    kinds of reject from staging. Returns the file stems for the two reject
    buckets so the caller can tell them apart:

      lossy   — a non-FLAC file (.mp3/.m4a/…). Qobuz served lossy because no
                lossless master is available for the user's tier; another
                source would be needed.
      broken  — a .flac that won't decode (truncated/interrupted download).
                A re-rip usually fixes it.
    """
    kept, lossy, broken = [], [], []
    for f in new_files:
        ext = f.suffix.lower()
        if ext == ".flac":
            if is_flac(f):
                kept.append(f)
                continue
            bucket, what = broken, "broken FLAC"
        elif ext in cfg.AUDIO_EXTS:
            bucket, what = lossy, "non-FLAC"
        else:
            continue
        # Record the reject whether or not we can delete it, so the caller's
        # counts and per-track retry see it. A reject left in staging gets
        # imported by beets (move: yes, autotag: no) — the exact lossy/truncated
        # file this discard exists to keep out — so when the unlink fails, move
        # it out of the import tree rather than leaving it to be picked up.
        bucket.append(f.stem)
        try:
            f.unlink()
        except OSError as e:
            if _quarantine_reject(f):
                vlog(f"set aside undeletable {what} {f.name} so it isn't imported")
            else:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Couldn't remove or set aside {what} {f.name}: {e}; "
                    "remove it from staging by hand before the next import."))
    return kept, lossy, broken


def _quarantine_reject(path):
    """Move a reject that wouldn't delete out of the staging import tree (into a
    quarantine under DATA_DIR) so beets can't pick it up. Returns True on a
    successful move."""
    try:
        rel = path.relative_to(cfg.STAGING_DIR)
    except ValueError:
        rel = Path(path.name)
    dest = cfg.DATA_DIR / ".rejected_staging" / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Quarantine persists across runs, so a same-named reject from a later
        # attempt would otherwise silently overwrite the earlier one — suffix
        # on collision instead.
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            n = 1
            while dest.exists():
                dest = dest.with_name(f"{stem}.{n}{suffix}")
                n += 1
        shutil.move(str(path), str(dest))
        return True
    except OSError:
        return False


# Known non-audio artifacts that streamrip leaves in STAGING_DIR between runs.
# These inflate the leftover count and can trigger the --yes abort threshold.
# .pdf covers booklets left behind by an older config (downloads are off now).
_RESIDUE_EXTS  = {".jpg", ".jpeg", ".png", ".gif", ".json", ".log", ".toml", ".pdf"}
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


def _dir_has_audio(d):
    """True if any audio file exists anywhere under d.

    Used to spare a real leftover album's art/metadata from the residue
    sweep — a cover.jpg beside the tracks is not an orphaned stray.
    """
    try:
        for child in d.rglob("*"):
            if child.is_file() and child.suffix.lower() in cfg.AUDIO_EXTS:
                return True
    except OSError:
        return False
    return False


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
    retry_dir_name = getattr(cfg, "BEETS_RETRY_DIR", None)
    # Walk dirs bottom-up so we can rmdir empty residue dirs after unlinking files.
    for p in sorted(cfg.STAGING_DIR.rglob("*"), key=lambda x: -len(x.parts)):
        # Skip the parked-retry subtree so a stash of albums waiting on a
        # manual retry doesn't get its cover art / .log files swept out from
        # under it.
        if retry_dir_name:
            try:
                rel = p.relative_to(cfg.STAGING_DIR)
                if rel.parts and rel.parts[0] == retry_dir_name:
                    continue
            except ValueError:
                pass
        try:
            if p.is_file() and p.suffix.lower() in _RESIDUE_EXTS:
                # Don't sweep art/metadata that belongs to a real leftover
                # album the user is about to import: a cover.jpg beside the
                # tracks is the filesystem fetchart source under
                # ARTWORK=sidecar. Only unlink residue whose containing album
                # dir holds no audio (an orphaned stray). Files directly in
                # STAGING_DIR root have no album dir, so they're always orphans.
                if p.parent != cfg.STAGING_DIR and _dir_has_audio(p.parent):
                    continue
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
