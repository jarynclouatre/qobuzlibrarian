"""beets import integration and staging pre-flight.

``beets_import_paths`` runs ``beet import`` directly and errors out if
``beet`` isn't on PATH — the staging pre-flight checks for it first.

Two non-obvious behaviours kept here:

- The on-disk override yaml is removed on every exit path (success,
  failure, KeyboardInterrupt). Otherwise a leftover override could
  silently affect a manual ``beet`` invocation later.
- ``clear_scan_caches()`` runs after import so post-import path lookups
  see the file move beets just performed (the in-memory listing cache
  would otherwise still point at the empty staging dir).
"""
import filecmp
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.library.scanner import clear_scan_caches
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.errors import EXIT_GENERAL, die
from qobuz_librarian.ui_cli.logging import log, report_progress, vlog

try:
    from mutagen.flac import FLAC as _MutagenFLAC  # noqa: F401
    HAVE_MUTAGEN = True
except Exception:
    HAVE_MUTAGEN = False


def _yaml_sq(value):
    """Emit *value* as a safe YAML single-quoted scalar.

    Single-quoted style is the simplest YAML scalar with no backslash
    escaping. The only metacharacter is the single quote itself, doubled
    to escape. Newlines aren't valid in these values; collapse to spaces
    so a stray one can't fold into a second YAML line.
    """
    s = str(value).replace("\r", " ").replace("\n", " ")
    return "'" + s.replace("'", "''") + "'"


def _files_identical(a, b):
    """Byte-for-byte equal? An unreadable file counts as not-identical so the
    caller stays on the conservative (keep-it) path."""
    try:
        return filecmp.cmp(str(a), str(b), shallow=False)
    except OSError:
        return False


def _merge_split_folder(dest_dir, source_dir):
    """Consolidate two folders: move files from source_dir into dest_dir.

    Conservative on overlaps: an audio file whose name already exists in the
    destination is left alone (it may be a different master). A non-audio
    duplicate (cover art, a sidecar) is dropped only when it's byte-identical
    to the destination copy, so a redundant file doesn't strand the source
    folder. beets' items.path is updated for each moved file so a later
    `beet update` doesn't read the move as a deletion. Returns the count of
    files moved.
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
    moved_pairs = []
    for src in list(source_dir.rglob("*")):
        if not src.is_file():
            continue
        try:
            rel = src.relative_to(source_dir)
        except ValueError:
            continue
        dst = dest_dir / rel
        if dst.exists():
            if (src.suffix.lower() not in cfg.AUDIO_EXTS
                    and _files_identical(src, dst)):
                try:
                    src.unlink()
                except OSError:
                    pass
            else:
                vlog(f"merge: skip {rel} — destination exists")
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved += 1
            moved_pairs.append((src, dst))
        except OSError as e:
            log.info(fmt(C.YELLOW, f"  ⚠  merge: couldn't move {src.name}: {e}."))
    if moved_pairs:
        from qobuz_librarian.library.catalog import _sync_beets_db_after_file_move
        for old, new in moved_pairs:
            _sync_beets_db_after_file_move(old, new)
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
        try:
            leftover = ", ".join(sorted(p.name for p in source_dir.iterdir()))
        except OSError:
            leftover = "?"
        vlog(f"merge: couldn't remove {source_dir} ({e}); "
             f"leftover: {leftover}")
        return moved
    try:
        source_parent.rmdir()
    except OSError:
        pass
    return moved


def _under_retry_dir(p):
    """True when p sits under STAGING_DIR/<BEETS_RETRY_DIR>/. Used to skip
    the parked-album quarantine when sweeping or counting staging."""
    try:
        rel = p.relative_to(cfg.STAGING_DIR)
    except ValueError:
        return False
    return rel.parts and rel.parts[0] == cfg.BEETS_RETRY_DIR


def _prepare_staging_tags(roots=None):
    """Quarantine untagged staged FLACs and clean the survivors' path tags in a
    single pass, so each file is opened by mutagen once rather than twice.

    A file that can't be read, or carries no album/artist tag, is moved to a
    timestamped quarantine dir under DATA_DIR — never deleted. beets files an
    untagged track under an empty-artist `/_/` folder, and a Qobuz download
    always carries tags, so a tagless staging file is almost always a
    cancelled/crashed-rip fragment; on the off chance it's a real file we just
    couldn't read, it stays recoverable (and can be retagged by hand). The rest
    get whitespace/quotes trimmed from the album/artist/title tags beets builds
    the on-disk path from — streamrip writes those from its own Qobuz fetch, so
    `Hunky Dory ` and `"Heroes"` would otherwise become the folder names.
    Returns the list of quarantined paths.

    Without mutagen every read fails; rather than quarantine the whole download
    as "untagged" and hand beets an empty dir, leave the files untouched.

    ``roots`` scopes the scan to those directories; None means the whole
    STAGING_DIR (back-compat for the preflight-driven full-batch import).
    """
    moved = []
    if not HAVE_MUTAGEN:
        return moved
    from mutagen.flac import FLAC

    from qobuz_librarian.library.tags import clean_qobuz_string
    scan_roots = roots if roots else [cfg.STAGING_DIR]
    try:
        flacs = []
        for r in scan_roots:
            flacs.extend(r.rglob("*.flac"))
        # Parked albums waiting on a manual retry shouldn't be re-quarantined
        # or re-tag-cleaned on every batch.
        flacs = [f for f in flacs if not _under_retry_dir(f)]
    except OSError:
        return moved
    quarantine = None
    for f in flacs:
        try:
            tags = FLAC(str(f))
        except Exception:
            tags = None
        aa = al = ""
        if tags is not None:
            aa = (tags.get("albumartist") or tags.get("artist") or [""])[0].strip()
            al = (tags.get("album") or [""])[0].strip()
        if tags is None or not aa or not al:
            if quarantine is None:
                quarantine = (cfg.DATA_DIR / ".untagged_staging"
                              / datetime.now().strftime("%Y%m%d_%H%M%S"))
            try:
                rel = f.relative_to(cfg.STAGING_DIR)
            except ValueError:
                rel = Path(f.name)
            dest = quarantine / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest))
                moved.append(f)
            except OSError as e:
                vlog(f"couldn't quarantine {f.name}: {e}")
            continue
        changed = False
        for key in ("album", "albumartist", "artist", "title"):
            vals = tags.get(key)
            if not vals:
                continue
            cleaned = [clean_qobuz_string(v) for v in vals]
            if cleaned != list(vals):
                tags[key] = cleaned
                changed = True
        if changed:
            try:
                tags.save()
            except Exception as e:
                vlog(f"couldn't rewrite cleaned tags on {f.name}: {e}")
    if moved:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Set aside {len(moved)} untagged file(s) → {quarantine}\n"
            "     (cancelled-rip leftovers, or files needing a manual retag — "
            "not deleted)."))
    return moved


def _build_import_override_yaml():
    """The beets config override the import runs with, as a YAML string.

    Forces the keys the import contract depends on — library/directory,
    non-interactive, non-incremental, and autotag off — and lets everything
    else fall through to the user's config.yaml. Autotag is forced off because
    streamrip already wrote authoritative Qobuz tags; matching them against
    MusicBrainz would, on anything but a confident match, skip the album
    outright under quiet mode and leave the files stranded in staging.
    Optional path templates, a plugins list, and album-art handling are
    derived from config. Emitting `plugins:` REPLACES the config.yaml list
    (beets doesn't merge lists), so it's only emitted when a plugins override
    or a non-default art mode requires it.
    """
    import re as _re
    override_yaml = (
        f"library: {_yaml_sq(cfg.BEETS_DB_PATH)}\n"
        f"directory: {_yaml_sq(cfg.MUSIC_ROOT)}\n"
        "import:\n"
        "  quiet: yes\n"
        "  incremental: no\n"
        "  autotag: no\n"
    )
    # Path templates are deployer-supplied and can contain single quotes
    # (e.g. `$albumartist's stuff/$album`); _yaml_sq keeps the scalar safe.
    _paths = []
    if cfg.BEETS_PATH_DEFAULT:
        _paths.append(f"  default: {_yaml_sq(cfg.BEETS_PATH_DEFAULT)}\n")
    if cfg.BEETS_PATH_SINGLETON:
        _paths.append(f"  singleton: {_yaml_sq(cfg.BEETS_PATH_SINGLETON)}\n")
    if cfg.BEETS_PATH_COMP:
        _paths.append(f"  comp: {_yaml_sq(cfg.BEETS_PATH_COMP)}\n")
    if _paths:
        override_yaml += "paths:\n" + "".join(_paths)

    # fetchart saves a cover file; embedart embeds it into the tracks. ARTWORK
    # picks which. Start from the user's plugin override (or the seeded
    # `fetchart` default) and add what the art mode needs.
    _plugins = list(cfg.BEETS_PLUGINS) if cfg.BEETS_PLUGINS else ["fetchart"]
    _art = getattr(cfg, "ARTWORK", "sidecar")
    # fetchart saves cover.jpg for sidecar too, so a custom BEETS_PLUGINS list
    # that omits it (and replaces config.yaml's) mustn't silently turn art off.
    if _art in ("sidecar", "embed", "both") and "fetchart" not in _plugins:
        _plugins.append("fetchart")
    if _art in ("embed", "both"):
        for _p in ("fetchart", "embedart"):
            if _p not in _plugins:
                _plugins.append(_p)
        # auto: embed after fetchart fetches the cover. remove_art_file drops
        # the on-disk cover for embed-only; both keeps it.
        override_yaml += ("embedart:\n"
                          "  auto: yes\n"
                          f"  remove_art_file: {'yes' if _art == 'embed' else 'no'}\n")
    if cfg.BEETS_PLUGINS or _art in ("embed", "both"):
        # Emitting `plugins:` replaces the config.yaml list, so re-add inline —
        # the seeded path template's `multidisc` field comes from it, and
        # dropping it would flatten multi-disc albums into one folder.
        if "inline" not in _plugins:
            _plugins.append("inline")
        # Plugin names must be plain identifiers — anything else would break
        # the YAML structure (and likely isn't a real beets plugin anyway).
        safe_plugins = [p for p in _plugins if _re.match(r"^[A-Za-z0-9_]+$", p)]
        if safe_plugins:
            override_yaml += f"plugins: [{', '.join(safe_plugins)}]\n"
    return override_yaml


def _prepare_for_beets_run(roots=None):
    """Common setup shared by the whole-staging and per-album entry points:
    require ``beet`` on PATH, prep tags on the staged FLACs, and write the
    one-shot override yaml. Returns ``(override_path, cleanup_fn)`` or
    ``(None, None)`` if a precondition failed (caller treats as error)."""
    if not shutil.which("beet"):
        log.info(fmt(C.RED, "  ✗  `beet` not found on PATH — beets is not installed in this environment."))
        log.info(fmt(C.GRAY, "     The bundled Docker image includes beets; bare-metal installs need it separately."))
        return None, None

    _prepare_staging_tags(roots=roots)

    user_config = cfg.BEETS_CONFIG_DIR / "config.yaml"
    if not user_config.exists():
        log.info(fmt(C.RED, f"  ✗  beets config not found at {user_config}."))
        log.info(fmt(C.GRAY, "     The container seeds a default on first start; check that the config volume is mounted writably."))
        return None, None

    override_path = cfg.BEETS_DB_PATH.parent / ".beets_import_override.yaml"
    try:
        override_path.parent.mkdir(parents=True, exist_ok=True)
        override_path.write_text(_build_import_override_yaml(), encoding="utf-8")
    except OSError as e:
        from qobuz_librarian.ui_cli.errors import oserr_hint
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't write beets override config: {e}.{oserr_hint(e)}"))
        override_path = None

    def _clean():
        if override_path:
            try:
                override_path.unlink(missing_ok=True)
            except OSError:
                pass

    return override_path, _clean


def beets_import_paths(consolidate=True):
    """Run beets import on the whole staging directory and return a bool.

    Used by the preflight "import then quarantine leftovers" recovery path.
    The queue executor calls ``beets_import_albums`` instead so per-album
    failures don't take the whole batch down.
    """
    override_path, cleanup = _prepare_for_beets_run()
    if cleanup is None:
        return False
    ok, _ = _beets_direct(override_path, cleanup, [str(cfg.STAGING_DIR)])
    if ok and consolidate:
        _consolidate_duplicate_albums()
    return ok


def beets_import_albums(album_dirs):
    """Run beets import scoped to ``album_dirs`` and return a tri-state code:

    - ``"ok"`` — import succeeded.
    - ``"timeout"`` — the idle-timeout guard fired; the executor decides
      whether to retry based on ``cfg.BEETS_MAX_ATTEMPTS``.
    - ``"error"`` — any other failure (config missing, non-zero exit,
      silent skip). The executor won't retry these.

    Consolidation is left to the caller (run once per batch instead of per
    album, since the duplicate-album fold is a library-wide pass).
    """
    if not album_dirs:
        return "ok"
    override_path, cleanup = _prepare_for_beets_run(roots=album_dirs)
    if cleanup is None:
        return "error"
    ok, kind = _beets_direct(override_path, cleanup, [str(d) for d in album_dirs])
    return "ok" if ok else kind


def _reap(proc):
    """Collect a killed child's exit status so it doesn't linger as a zombie
    until the Popen object is garbage-collected."""
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


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


def _count_audio_under(paths):
    """Total audio files under the given import paths, ignoring the parked-retry
    subtree. beets imports by moving files into the library, so a before/after
    count tells a real import (the staged tracks left) from an exit-0 run that
    moved nothing."""
    exts = set(cfg.AUDIO_EXTS)
    n = 0
    for p in paths:
        root = Path(p)
        if not root.exists():
            continue
        try:
            for f in root.rglob("*"):
                if (f.is_file() and f.suffix.lower() in exts
                        and not _under_retry_dir(f)):
                    n += 1
        except OSError:
            continue
    return n


def _beets_direct(override_path, cleanup_fn, paths=None):
    """Call ``beet import`` as a direct subprocess (bundled mode), scoped to
    the given paths (default: whole STAGING_DIR for back-compat). Returns
    ``(ok: bool, kind: str)`` where kind is ``"ok"`` / ``"timeout"`` /
    ``"error"`` so the caller can distinguish a retry-worthy idle-timeout
    from a permanent failure."""
    if not paths:
        paths = [str(cfg.STAGING_DIR)]
    audio_before = _count_audio_under(paths)
    cmd = ["beet"]
    if override_path:
        cmd += ["-c", str(override_path)]
    cmd += ["import", *paths]

    # Total album count when the caller passed explicit paths; 0 (= indeterminate
    # progress bar) when the whole staging dir is being scanned.
    staging_root = str(cfg.STAGING_DIR).rstrip("/")
    explicit_album_paths = [p for p in paths if p.rstrip("/") != staging_root]
    total_albums = len(explicit_album_paths)
    report_progress("Importing into your library", 0, total_albums, "")
    log.info(fmt(C.CYAN, "  ⟳  Running beets import ..."))
    out_lines = []
    last_output = [time.monotonic()]
    # Captured by _reader for per-album progress updates — list-of-1 so the
    # nested function can mutate without `nonlocal`.
    last_album = [None]
    seen_albums = [0]

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
            # Own session so a terminal Ctrl-C doesn't hit beets mid-import
            # directly — the interrupt handler below kills it through the
            # controlled path (cleanup + cache clear), like rip.py does.
            start_new_session=True,
        )
    except OSError:
        log.info(fmt(C.RED, "  ✗  `beet` not found. Is beets installed?"))
        cleanup_fn()
        return False, "error"

    def _reader():
        prefix = staging_root + "/"
        for line in proc.stdout:
            out_lines.append(line)
            last_output[0] = time.monotonic()
            stripped = line.strip()
            if not stripped:
                continue
            # Staging-path echoes are too noisy for the log, but they're the
            # signal that tells us which album beets is on. Pull the first
            # path segment after the staging root (the album folder) and use
            # it as the live "Importing: …" subtitle. Same album seen twice
            # in a row doesn't tick the counter — beets often prints the path
            # several times for one album.
            idx = stripped.find(prefix)
            if idx >= 0:
                rest = stripped[idx + len(prefix):]
                album = rest.split("/", 1)[0].split(" (", 1)[0].strip()
                if album and album != last_album[0]:
                    last_album[0] = album
                    seen_albums[0] += 1
                    report_progress("Importing into your library",
                                    seen_albums[0], total_albums, album)
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
        _reap(proc)
        reader.join(timeout=5)
        cleanup_fn()
        clear_scan_caches()
        log.info(fmt(C.RED,
            f"  ✗  beets import produced no output for {cfg.BEETS_TIMEOUT}s "
            f"— assuming hung, killed. Files remain in staging for a "
            f"manual retry. (Raise/disable with BEETS_TIMEOUT.)"))
        _report_staging_remnants()
        return False, "timeout"
    except KeyboardInterrupt:
        proc.kill()
        _reap(proc)
        reader.join(timeout=5)
        cleanup_fn()
        clear_scan_caches()
        raise

    reader.join(timeout=5)
    full_out = "".join(out_lines)
    cleanup_fn()

    # beets moves audio out of staging into the library, so the honest success
    # signal is whether the staged tracks actually left — not a phrase in beets'
    # output. beets prints "Skipping." per item (a duplicate, an unreadable
    # file), so matching that text flagged a whole album failed when a single
    # track was skipped; an exit-0 run that moved nothing is the real silent skip.
    audio_after = _count_audio_under(paths)
    moved_any = audio_before > 0 and audio_after < audio_before
    imported_nothing = proc.returncode == 0 and audio_before > 0 and not moved_any

    clear_scan_caches()

    # Prune empty staging dirs after move. Skip the parked-retry subtree so a
    # legitimate stash of failed albums isn't quietly demoted to nothing.
    for _d in sorted(cfg.STAGING_DIR.rglob("*"), key=lambda x: -len(x.parts)):
        if _d.is_dir() and not _under_retry_dir(_d):
            try: _d.rmdir()
            except OSError: pass

    if proc.returncode == 0 and not imported_nothing:
        log.info(fmt(C.GREEN, "  ✓  beets import succeeded."))
        if audio_after:
            log.info(fmt(C.GRAY,
                f"     {audio_after} staged track(s) weren't imported "
                "(likely duplicates or unreadable); left in staging."))
        return True, "ok"
    if imported_nothing:
        log.info(fmt(C.RED, "  ✗  beets exited 0 but moved nothing into the library."))
        log.info(fmt(C.GRAY, "     Files remain in staging. Try manually:"))
        log.info(fmt(C.GRAY, f"     {' '.join(cmd)}"))
        _report_staging_remnants()
        return False, "error"
    log.info(fmt(C.RED, f"  ✗  beets exited {proc.returncode}."))
    log.info(fmt(C.GRAY, f"     Config: {cfg.BEETS_CONFIG_DIR}/config.yaml"))
    if override_path:
        log.info(fmt(C.GRAY, f"     Override: {override_path}"))
    for line in full_out.splitlines()[-20:]:
        log.info(fmt(C.GRAY, f"    {line}"))
    _report_staging_remnants()
    return False, "error"


def _report_staging_remnants():
    """List the top-level album folders still in staging after a beets
    failure so the user knows exactly what didn't import. A bare
    "files remain in staging" line leaves them guessing across batches
    of 100+ albums."""
    try:
        folders = sorted(p for p in cfg.STAGING_DIR.iterdir()
                         if p.is_dir() and p.name != cfg.BEETS_RETRY_DIR)
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


# Unit separator — can't appear in a filesystem path or a tag value, so it's
# a safe field delimiter for parsing `beet ls -f` output.
_ALBUM_FIELD_SEP = "\x1f"


def _duplicate_album_dirs(listing):
    """Directories that hold more than one beets album row for the SAME album.

    `listing` is the output of
    ``beet ls -a -f '$path<sep>$albumartist<sep>$album'``. A directory is
    returned only when every row in it shares one (albumartist, album); a
    directory where two genuinely different albums coexist is left alone.
    """
    by_dir: dict = {}
    for line in listing.splitlines():
        if line.count(_ALBUM_FIELD_SEP) != 2:
            continue
        path, artist, album = line.split(_ALBUM_FIELD_SEP)
        if path:
            by_dir.setdefault(path, []).append((artist, album))
    return [d for d, rows in by_dir.items()
            if len(rows) > 1 and len(set(rows)) == 1]


def _untracked_reimport_file():
    return cfg.DATA_DIR / ".beets_untracked_reimport"


def _park_untracked_for_reimport(path):
    """Record a library folder that a consolidation re-import left untracked in
    beets (remove succeeded, re-import failed — usually a transient DB lock), so
    the next consolidation pass retries tracking it. The audio is safe on disk;
    this only gets it back into the beets database."""
    f = _untracked_reimport_file()
    try:
        existing = set()
        if f.exists():
            existing = {ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                        if ln.strip()}
        existing.add(str(path))
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("\n".join(sorted(existing)) + "\n", encoding="utf-8")
    except OSError:
        pass


def _unpark_reimport(path):
    """Drop a path the fold parked as a safety net, once it's safely tracked
    again (or the destructive remove that needed the net never ran)."""
    f = _untracked_reimport_file()
    if not f.exists():
        return
    try:
        kept = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                if ln.strip() and ln.strip() != str(path)]
        if kept:
            f.write_text("\n".join(sorted(set(kept))) + "\n", encoding="utf-8")
        else:
            f.unlink(missing_ok=True)
    except OSError:
        pass


def _reimport_untracked_library_dirs(base, beet_env, idle):
    """Retry `beet import -A` on library folders a prior consolidation stranded.
    Drops each path that now tracks (or no longer exists); keeps the ones still
    failing for the next pass. Mirrors the `.beets_retry/` guarantee for the
    library-side strand the remove+import dance can leave behind."""
    f = _untracked_reimport_file()
    if not f.exists():
        return
    try:
        paths = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
    except OSError:
        return
    still_failing = []
    for p in paths:
        if not Path(p).exists():
            continue  # folder deleted/moved since — nothing left to track
        try:
            imp = subprocess.run(base + ["import", "-A", p], capture_output=True,
                                 text=True, env=beet_env, timeout=idle)
        except (OSError, subprocess.SubprocessError):
            still_failing.append(p)
            continue
        if imp.returncode == 0:
            log.info(fmt(C.GREEN,
                f"  ✓  Re-tracked a previously-stranded album: {Path(p).name}"))
        else:
            still_failing.append(p)
    try:
        if still_failing:
            f.write_text("\n".join(sorted(set(still_failing))) + "\n", encoding="utf-8")
        else:
            f.unlink(missing_ok=True)
    except OSError:
        pass


def _consolidate_duplicate_albums():
    """Fold any album folder that beets split into several album rows into one.

    beets opens a fresh album row when an import drops tracks into a folder
    that already holds an album — exactly what gap-fill, repair, and
    re-downloads do, so the library ends up listing the album (and any
    refilled track) more than once. beets' own `duplicate_action: merge` does
    not combine these as-is imports, so reconcile with remove+import, which
    leaves the database schema for beets to manage rather than hand-editing
    rows. A folder is only touched when every row in it is the SAME album, so
    two genuinely different albums sharing a directory are never merged.
    """
    if not shutil.which("beet"):
        return
    try:
        override_path = cfg.BEETS_DB_PATH.parent / ".beets_consolidate_override.yaml"
        override_path.write_text(_build_import_override_yaml(), encoding="utf-8")
    except OSError:
        return
    beet_env = {**os.environ, "BEETSDIR": str(cfg.BEETS_CONFIG_DIR)}
    base = ["beet", "-c", str(override_path)]
    # Wall-clock cap for the consolidation imports. BEETS_TIMEOUT <= 0 means
    # "no guard" (subprocess.run's sentinel is None, not 0 — 0 would fire
    # instantly), matching how the main per-album import path honours 0.
    idle = cfg.BEETS_TIMEOUT if cfg.BEETS_TIMEOUT and cfg.BEETS_TIMEOUT > 0 else None

    def _drop_override():
        try:
            override_path.unlink(missing_ok=True)
        except OSError:
            pass

    # Retry anything a previous pass stranded before reading the library, so a
    # transient failure self-heals on the next run rather than lingering.
    _reimport_untracked_library_dirs(base, beet_env, idle)

    try:
        listed = subprocess.run(
            base + ["ls", "-a", "-f",
                    f"$path{_ALBUM_FIELD_SEP}$albumartist{_ALBUM_FIELD_SEP}$album"],
            capture_output=True, text=True, env=beet_env, timeout=idle,
        )
    except (OSError, subprocess.SubprocessError):
        _drop_override()
        return
    # A nonzero exit (locked DB, a config/plugin error) yields empty stdout that
    # would otherwise read as "no duplicates" and skip the fold silently.
    if listed.returncode != 0:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't read the beets library to fold duplicates "
            f"(beet ls exited {listed.returncode}); skipping this run."))
        if listed.stderr.strip():
            vlog(f"beet ls stderr: {listed.stderr.strip()[:500]}")
        _drop_override()
        return

    dup_dirs = _duplicate_album_dirs(listed.stdout)

    if dup_dirs:
        log.info(fmt(C.GRAY,
            f"  ⤷  Tidying {len(dup_dirs)} album folder(s) beets had split "
            "into duplicate library entries…"))
    for d in dup_dirs:
        try:
            # Park BEFORE the destructive remove. The remove clears every row
            # for the folder, so a hard kill (OOM, power loss, a docker stop
            # past its grace timeout) between the remove and the re-import would
            # otherwise strand the album — files on disk, absent from beets,
            # with nothing scheduled to re-track it. Parked first, the next
            # pass's _reimport_untracked_library_dirs picks it up; the unparks
            # below drop it again the moment it's safely tracked.
            _park_untracked_for_reimport(d)
            rm = subprocess.run(base + ["remove", "-f", "path:" + d],
                                capture_output=True, text=True, env=beet_env,
                                timeout=120)
            # If the remove failed the rows are still there; re-importing on top
            # would add a third row and make the split worse, so skip this one.
            if rm.returncode != 0:
                _unpark_reimport(d)  # nothing was removed — nothing to recover
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Couldn't clear the old entries for {d} (beet remove "
                    f"exited {rm.returncode}); leaving it as-is. Run "
                    f"`beet remove` then `beet import` on that folder by hand."))
                continue
            # The remove just cleared every row for this folder; if the
            # re-import fails the album is left untracked, which is worse than
            # the duplicate. Surface that loudly so it can be re-imported.
            # -A (as-is, no autotag): consolidation only folds duplicate rows
            # back together — it must keep the tags the tracks already have, not
            # re-guess them against MusicBrainz (which a user's autotag:yes
            # config would otherwise trigger on the whole album).
            imp = subprocess.run(base + ["import", "-A", d],
                                 capture_output=True, text=True, env=beet_env,
                                 timeout=idle)
            if imp.returncode != 0:
                # A transient DB lock is the usual cause — retry once before
                # leaving it parked for the next pass.
                imp = subprocess.run(base + ["import", "-A", d],
                                     capture_output=True, text=True,
                                     env=beet_env, timeout=idle)
            if imp.returncode == 0:
                _unpark_reimport(d)  # re-tracked cleanly — drop the safety net
            else:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  {d} couldn't be re-tracked in beets after "
                    f"de-duplicating (re-import exited {imp.returncode} twice — "
                    f"usually a transient DB lock). The files are safe on disk; "
                    f"the next download run retries this automatically. To do it "
                    f"now: beet import -A \"{d}\""))
        except (OSError, subprocess.SubprocessError) as e:
            # Left parked (recorded above) so the next pass retries tracking it.
            log.info(fmt(C.YELLOW,
                f"  ⚠  Couldn't tidy duplicate entries for {d} ({e}); "
                "the next download run retries this automatically."))

    _drop_override()
    if dup_dirs:
        clear_scan_caches()


def forget_beets_entries(paths):
    """Drop beets DB entries for files that were deleted outside beets.

    Consolidation removes duplicate sibling tracks straight off disk; without
    this their rows linger in the beets library, so someone who also runs
    `beet` by hand sees ghost tracks until the next `beet update`. `beet
    remove` is used rather than a direct sqlite delete so beets cleans up the
    album row and any flexible-attribute rows along with the item — leaving
    the database consistent. No `-d`: the files are already gone, so beets
    only touches its database. `-l` pins the same database the import wrote to
    (the import override forces `library:` to BEETS_DB_PATH), so this stays
    correct even when a deployer points BEETS_DB_PATH somewhere other than the
    `library:` line in config.yaml.

    The paths are OR'd into one query — a bare comma separates beets
    sub-queries — so a consolidation that deletes dozens of tracks costs two
    `beet` runs, not one per file. A `list` pass first counts how many of the
    paths beets actually tracks: that count is what's returned (0, with no
    `remove`, when beets knows none of them). beets' path query is
    directory-boundary aware, so each full file path matches exactly its track.
    """
    paths = [str(p) for p in paths if str(p)]
    if not paths or not shutil.which("beet"):
        return 0
    beet_env = {**os.environ, "BEETSDIR": str(cfg.BEETS_CONFIG_DIR)}
    base = ["beet", "-l", str(cfg.BEETS_DB_PATH)]
    query = []
    for p in paths:
        if query:
            query.append(",")
        query.append("path:" + p)
    def _ghost_warning(reason):
        # The files are already gone from disk, so a remove that can't run
        # leaves their rows behind as ghost entries in `beet ls` until a
        # hand-run `beet update` notices the missing files. Returning 0
        # silently reads as "nothing to forget", so say what happened.
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't drop the deleted track(s) from the beets library "
            f"({reason}); they'll linger as ghost entries until you run "
            f"`beet update`."))

    try:
        listing = subprocess.run(base + ["ls", "-f", "$id", *query],
                                 capture_output=True, text=True,
                                 env=beet_env, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        _ghost_warning(f"couldn't read the library — {e}")
        return 0
    tracked = sum(1 for line in listing.stdout.splitlines() if line.strip())
    if not tracked:
        return 0
    try:
        rm = subprocess.run(base + ["remove", "-f", *query],
                            capture_output=True, text=True,
                            env=beet_env, timeout=120)
        # A nonzero exit is usually a transient DB lock — retry once before
        # falling back to the manual-recovery message (mirrors the
        # consolidation re-import retry above).
        if rm.returncode != 0:
            rm = subprocess.run(base + ["remove", "-f", *query],
                                capture_output=True, text=True,
                                env=beet_env, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        _ghost_warning(f"beet remove couldn't run — {e}")
        return 0
    if rm.returncode == 0:
        return tracked
    _ghost_warning(f"beet remove exited {rm.returncode}")
    return 0


def staging_preflight(args):
    """Sweep streamrip non-audio residue, then handle any remaining leftovers."""
    from qobuz_librarian.integrations.rip import cleanup_staging_residue

    if not cfg.STAGING_DIR.exists():
        cfg.STAGING_DIR.mkdir(parents=True, exist_ok=True)
        return

    n_swept = cleanup_staging_residue()
    if n_swept:
        vlog(f"staging_preflight: swept {n_swept} non-audio residue item(s)")

    files = [f for f in cfg.STAGING_DIR.rglob("*")
             if f.is_file() and not _under_retry_dir(f)]
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
        sys.exit(EXIT_GENERAL)

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
        from qobuz_librarian.integrations.downsample_engine import (
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
            from qobuz_librarian.integrations.lyrics import _run_lyric_hook
            _run_lyric_hook(cfg.STAGING_DIR)
        except (KeyboardInterrupt, Exception) as _le:
            if isinstance(_le, KeyboardInterrupt):
                sys.exit(EXIT_GENERAL)
            log.info(fmt(C.YELLOW, f"  ⚠  lyric hook failed during preflight: {_le}."))
        if not beets_import_paths():
            die(fmt(C.RED, "  beets import failed; aborting."), EXIT_GENERAL)
        leftover = [f for f in cfg.STAGING_DIR.rglob("*")
                    if f.is_file() and not _under_retry_dir(f)]
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
                # Keep each file's staging subpath so two albums with a
                # same-named file (cover.jpg, "01 - Track.flac") don't collide
                # and silently overwrite each other in the flat orphan dir.
                try:
                    rel = f.relative_to(cfg.STAGING_DIR)
                except ValueError:
                    rel = Path(f.name)
                dest = orphan_dir / rel
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(dest))
                except OSError:
                    pass
    elif r == "3":
        log.info(fmt(C.GRAY, "  Proceeding with leftovers in place."))
    else:
        die(fmt(C.GRAY, "  Aborted."), EXIT_GENERAL)
