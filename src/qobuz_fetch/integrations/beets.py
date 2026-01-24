"""beets import integration and staging pre-flight.

``beets_import_paths`` calls ``beet`` directly when it's in PATH (the
bundled / Docker case). For dev setups where beets lives in a separate
compose service, it falls back to ``docker compose exec`` so the same
function still works end-to-end.

Two non-obvious behaviours kept here:

- The on-disk override yaml is removed on every exit path (success,
  failure, KeyboardInterrupt). Otherwise a leftover override could
  silently affect a manual ``beet`` invocation later.
- ``clear_scan_caches()`` runs after import so post-import path lookups
  see the file move beets just performed (the in-memory listing cache
  would otherwise still point at the empty staging dir).
"""
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from qobuz_fetch import config as cfg
from qobuz_fetch.library.scanner import clear_scan_caches
from qobuz_fetch.ui_cli.colors import C, fmt
from qobuz_fetch.ui_cli.errors import EXIT_AUTH, EXIT_GENERAL, die
from qobuz_fetch.ui_cli.logging import log, vlog


def _yaml_sq(value):
    """Emit *value* as a safe YAML single-quoted scalar.

    Single-quoted style is the simplest YAML scalar with no backslash
    escaping. The only metacharacter is the single quote itself, doubled
    to escape. Newlines aren't valid in these values; collapse to spaces
    so a stray one can't fold into a second YAML line.
    """
    s = str(value).replace("\r", " ").replace("\n", " ")
    return "'" + s.replace("'", "''") + "'"


def _merge_split_folder(dest_dir, source_dir):
    """Consolidate two folders: move all files from source_dir into dest_dir.

    Conservative on overlaps: if a destination filename already exists,
    we leave the source in place. Returns the count of files moved.
    """
    if not isinstance(dest_dir, Path) or not isinstance(source_dir, Path):
        return 0
    if not source_dir.exists() or not source_dir.is_dir():
        return 0
    try:
        if dest_dir.resolve() == source_dir.resolve():
            return 0
    except OSError:
        return 0
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  merge: couldn't create {dest_dir}: {e}."))
        return 0
    moved = 0
    for src in list(source_dir.rglob("*")):
        if not src.is_file():
            continue
        try:
            rel = src.relative_to(source_dir)
        except ValueError:
            continue
        dst = dest_dir / rel
        if dst.exists():
            vlog(f"merge: skip {rel} — destination exists")
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved += 1
        except OSError as e:
            log.info(fmt(C.YELLOW, f"  ⚠  merge: couldn't move {src.name}: {e}."))
    for d in sorted([p for p in source_dir.rglob("*") if p.is_dir()],
                    key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    source_parent = source_dir.parent
    try:
        source_dir.rmdir()
    except OSError as e:
        vlog(f"merge: couldn't remove {source_dir} ({e})")
        return moved
    try:
        source_parent.rmdir()
    except OSError:
        pass
    return moved


def beets_import_paths():
    """Run beets import on the staging directory.

    Requires `beet` to be on PATH (the bundled Docker image guarantees this).
    """
    # ── Require beet on PATH ──────────────────────────────────────────────────
    if not shutil.which("beet"):
        log.info(fmt(C.RED, "  ✗  `beet` not found on PATH — beets is not installed in this environment."))
        log.info(fmt(C.GRAY, "     The bundled Docker image includes beets; bare-metal installs need it separately."))
        return False

    user_config = cfg.BEETS_CONFIG_DIR / "config.yaml"
    if not user_config.exists():
        log.info(fmt(C.RED, f"  ✗  beets config not found at {user_config}."))
        log.info(fmt(C.GRAY, "     The container seeds a default on first start; check that the config volume is mounted writably."))
        return False

    # ── Override config ───────────────────────────────────────────────────────
    # We always write an override yaml so the import uses the right settings
    # regardless of whatever the user has in their main beets config.
    BEETS_OVERRIDE_NAME = ".beets_import_override.yaml"
    override_path = cfg.BEETS_DB_PATH.parent / BEETS_OVERRIDE_NAME

    # Only force the keys the app genuinely needs for correctness; let
    # everything else fall through to the user's /config/beets/config.yaml
    # so toggles like `autotag`, `move`, `duplicate_action` are actually
    # editable (the README documents them as user-tunable).
    #
    # Forced here:
    #   library / directory  — the app dictates where the DB and music live
    #   import.quiet         — non-interactive run; never block on a prompt
    #   import.incremental   — must be false so a retry sees the same files
    override_yaml = (
        f"library: {_yaml_sq(cfg.BEETS_DB_PATH)}\n"
        f"directory: {_yaml_sq(cfg.MUSIC_ROOT)}\n"
        "import:\n"
        "  quiet: yes\n"
        "  incremental: no\n"
    )

    # Optional naming overrides. Only emit keys the user actually set, so
    # an unset format falls through to their config.yaml / beets defaults.
    # The values are deployer-supplied beets path templates — realistically
    # things like `$albumartist's stuff/$album`, i.e. they contain single
    # quotes. _yaml_sq emits a safe single-quoted scalar so a quote can't
    # break the file or inject directives.
    _paths = []
    if cfg.BEETS_PATH_DEFAULT:
        _paths.append(f"  default: {_yaml_sq(cfg.BEETS_PATH_DEFAULT)}\n")
    if cfg.BEETS_PATH_SINGLETON:
        _paths.append(f"  singleton: {_yaml_sq(cfg.BEETS_PATH_SINGLETON)}\n")
    if cfg.BEETS_PATH_COMP:
        _paths.append(f"  comp: {_yaml_sq(cfg.BEETS_PATH_COMP)}\n")
    if _paths:
        override_yaml += "paths:\n" + "".join(_paths)

    # Optional plugins override. Unset = honour whatever the user's
    # config.yaml has (seeded with `fetchart` only). Setting this replaces
    # the plugins list entirely; beets config layering doesn't merge lists.
    if cfg.BEETS_PLUGINS:
        # Plugin names must be plain identifiers — anything else would break
        # the YAML structure (and likely isn't a real beets plugin anyway).
        import re as _re
        safe_plugins = [p for p in cfg.BEETS_PLUGINS if _re.match(r"^[A-Za-z0-9_]+$", p)]
        if safe_plugins:
            override_yaml += f"plugins: [{', '.join(safe_plugins)}]\n"

    try:
        override_path.parent.mkdir(parents=True, exist_ok=True)
        override_path.write_text(override_yaml, encoding="utf-8")
    except OSError as e:
        from qobuz_fetch.ui_cli.errors import oserr_hint
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't write beets override config: {e}.{oserr_hint(e)}"))
        override_path = None

    def _clean():
        if override_path:
            try:
                override_path.unlink(missing_ok=True)
            except OSError:
                pass

    return _beets_direct(override_path, _clean)


def _wait_or_idle_kill(proc, last_output):
    """Block until proc exits. Raise subprocess.TimeoutExpired ONLY if it
    emits no output for cfg.BEETS_TIMEOUT seconds — a genuinely hung
    import (DB lock, an unexpected prompt, a deadlocked plugin).

    Crucially this is an *inactivity* timeout, not a wall-clock one: a
    legitimately slow import (a large library over R2 / a slow NAS)
    keeps printing beets progress lines, so its idle gap stays small and
    it is never killed no matter how many hours it runs. Only true
    silence trips it. cfg.BEETS_TIMEOUT <= 0 disables the guard entirely
    (plain wait(), the original never-kill behaviour).

    `last_output` is a 1-element list holding the monotonic timestamp of
    the most recent output line, updated by the reader thread.
    """
    idle_limit = cfg.BEETS_TIMEOUT
    while True:
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            if idle_limit and idle_limit > 0 and (
                    time.monotonic() - last_output[0] > idle_limit):
                raise  # no output for idle_limit s → assume hung
            # still running and recently active (or guard disabled) → wait


def _beets_direct(override_path, cleanup_fn):
    """Call `beet import` as a direct subprocess (bundled mode)."""
    cmd = ["beet"]
    if override_path:
        cmd += ["-c", str(override_path)]
    cmd += ["import", str(cfg.STAGING_DIR)]

    log.info(fmt(C.CYAN, "  ⟳  Running beets import ..."))
    out_lines = []
    last_output = [time.monotonic()]

    beet_env = {**os.environ, "BEETSDIR": str(cfg.BEETS_CONFIG_DIR)}
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=beet_env,
        )
    except OSError:
        log.info(fmt(C.RED, "  ✗  `beet` not found. Is beets installed?"))
        cleanup_fn()
        return False

    def _reader():
        for line in proc.stdout:
            out_lines.append(line)
            last_output[0] = time.monotonic()
            stripped = line.strip()
            if not stripped:
                continue
            # Suppress staging-path echo lines (noise)
            if str(cfg.STAGING_DIR) in stripped:
                continue
            # Route through the shared logger so the web UI's SSE stream
            # sees beets output too (raw sys.stdout would only land in
            # docker logs, leaving the job page silent during import).
            log.info(fmt(C.GRAY, "    " + line.rstrip()))

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    try:
        _wait_or_idle_kill(proc, last_output)
    except subprocess.TimeoutExpired:
        # No output for BEETS_TIMEOUT s → genuinely hung (a slow but
        # progressing import keeps printing, so it never reaches here).
        # Left unbounded this freezes the single web job worker for good.
        proc.kill()
        reader.join(timeout=5)
        cleanup_fn()
        clear_scan_caches()
        log.info(fmt(C.RED,
            f"  ✗  beets import produced no output for {cfg.BEETS_TIMEOUT}s "
            f"— assuming hung, killed. Files remain in staging for a "
            f"manual retry. (Raise/disable with BEETS_TIMEOUT.)"))
        _report_staging_remnants()
        return False
    except KeyboardInterrupt:
        proc.kill()
        reader.join(timeout=5)
        cleanup_fn()
        clear_scan_caches()
        raise

    reader.join(timeout=5)
    full_out = "".join(out_lines)
    cleanup_fn()

    skipped_silently = (
        proc.returncode == 0
        and ("No files imported" in full_out or "Skipping." in full_out)
    )

    clear_scan_caches()

    # Prune empty staging dirs after move
    for _d in sorted(cfg.STAGING_DIR.rglob("*"), key=lambda x: -len(x.parts)):
        if _d.is_dir():
            try: _d.rmdir()
            except OSError: pass

    if proc.returncode == 0 and not skipped_silently:
        log.info(fmt(C.GREEN, "  ✓  beets import succeeded."))
        return True
    if skipped_silently:
        log.info(fmt(C.RED, "  ✗  beets exited 0 but imported nothing (silent skip)."))
        log.info(fmt(C.GRAY, "     Files remain in staging. Try manually:"))
        log.info(fmt(C.GRAY, f"     {' '.join(cmd)}"))
        _report_staging_remnants()
        return False
    log.info(fmt(C.RED, f"  ✗  beets exited {proc.returncode}."))
    log.info(fmt(C.GRAY, f"     Config: {cfg.BEETS_CONFIG_DIR}/config.yaml"))
    if override_path:
        log.info(fmt(C.GRAY, f"     Override: {override_path}"))
    for line in full_out.splitlines()[-20:]:
        log.info(fmt(C.GRAY, f"    {line}"))
    _report_staging_remnants()
    return False


def _report_staging_remnants():
    """List the top-level album folders still in staging after a beets
    failure so the user knows exactly what didn't import. A bare
    "files remain in staging" line leaves them guessing across batches
    of 100+ albums."""
    try:
        folders = sorted(p for p in cfg.STAGING_DIR.iterdir() if p.is_dir())
    except OSError:
        return
    if not folders:
        return
    log.info(fmt(C.GRAY,
        f"     Staged folders remaining ({len(folders)}):"))
    audio_exts = set(cfg.AUDIO_EXTS)
    for folder in folders[:20]:
        try:
            n_audio = sum(1 for f in folder.rglob("*")
                          if f.is_file() and f.suffix.lower() in audio_exts)
        except OSError:
            n_audio = 0
        log.info(fmt(C.GRAY, f"       · {folder.name} ({n_audio} track(s))"))
    if len(folders) > 20:
        log.info(fmt(C.GRAY, f"       … and {len(folders) - 20} more"))


def staging_preflight(args):
    """Sweep streamrip non-audio residue, then handle any remaining leftovers."""
    from qobuz_fetch.integrations.rip import cleanup_staging_residue

    if not cfg.STAGING_DIR.exists():
        cfg.STAGING_DIR.mkdir(parents=True, exist_ok=True)
        return

    n_swept = cleanup_staging_residue()
    if n_swept:
        vlog(f"staging_preflight: swept {n_swept} non-audio residue item(s)")

    files = [f for f in cfg.STAGING_DIR.rglob("*") if f.is_file()]
    if not files:
        return

    log.info(fmt(C.YELLOW, f"\n  ⚠  Staging dir not empty: {len(files)} file(s) at {cfg.STAGING_DIR}."))
    sample = [str(f.relative_to(cfg.STAGING_DIR)) for f in files[:5]]
    for s in sample:
        log.info(fmt(C.GRAY, f"     {s}"))
    if len(files) > 5:
        log.info(fmt(C.GRAY, f"     ... and {len(files) - 5} more"))

    if args.yes:
        if len(files) <= cfg.LEFTOVER_WARN_LIMIT:
            log.info(fmt(C.YELLOW,
                f"  ⚠  --yes: bundling {len(files)} leftover file(s) into this import."))
            return
        log.info(fmt(C.RED + C.BOLD,
            f"\n  ✗  Refusing --yes silent bypass: {len(files)} files exceeds threshold."))
        sys.exit(EXIT_AUTH)

    print()
    log.info(fmt(C.CYAN, "  Options:"))
    log.info(fmt(C.WHITE, "    1) Run beets now to clear staging, then proceed"))
    log.info(fmt(C.WHITE, "    2) Abort"))
    log.info(fmt(C.WHITE, "    3) Proceed anyway (leftovers bundled with new files)"))
    try:
        r = input(fmt(C.CYAN, "  Choice [1/2/3]: ")).strip()
    except EOFError:
        r = "2"
    if r == "1":
        from qobuz_fetch.integrations.compress import (
            HAVE_DOWNSAMPLE,
            downsample_dir,
        )
        if HAVE_DOWNSAMPLE and not getattr(args, "no_downsample",
                                           getattr(args, "no_compress", False)):
            try:
                downsample_dir(cfg.STAGING_DIR, verbose=True, base_dir=cfg.STAGING_DIR, log=log.info)
            except (KeyboardInterrupt, Exception) as _ce:
                if isinstance(_ce, KeyboardInterrupt):
                    sys.exit(EXIT_GENERAL)
                log.info(fmt(C.YELLOW, f"  ⚠  downsample hook failed during preflight: {_ce}."))
        try:
            from qobuz_fetch.integrations.lyrics import _run_lyric_hook
            _run_lyric_hook(cfg.STAGING_DIR)
        except (KeyboardInterrupt, Exception) as _le:
            if isinstance(_le, KeyboardInterrupt):
                sys.exit(EXIT_GENERAL)
            log.info(fmt(C.YELLOW, f"  ⚠  lyric hook failed during preflight: {_le}."))
        if not beets_import_paths():
            die(fmt(C.RED, "  beets import failed; aborting."), EXIT_GENERAL)
        leftover = [f for f in cfg.STAGING_DIR.rglob("*") if f.is_file()]
        if leftover:
            orphan_dir = (cfg.STAGING_DIR.parent / ".staging.orphans"
                          / datetime.now().strftime("%Y%m%d_%H%M%S"))
            try:
                orphan_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                die(fmt(C.RED, f"  ✗  Couldn't create orphan dir: {e}."),
                    EXIT_GENERAL)
            log.info(fmt(C.YELLOW,
                f"  ⚠  Quarantining {len(leftover)} unimportable file(s) → {orphan_dir}."))
            for f in leftover:
                try:
                    shutil.move(str(f), str(orphan_dir / f.name))
                except OSError:
                    pass
    elif r == "3":
        log.info(fmt(C.GRAY, "  Proceeding with leftovers in place."))
    else:
        die(fmt(C.GRAY, "  Aborted."), EXIT_GENERAL)
