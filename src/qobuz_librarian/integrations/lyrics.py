"""Lyric fetch integration — pre-import hook, retry manifest, state pruning.

"""
import json
import os
import sys as _sys
from datetime import datetime
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log, vlog

# lyric_fetch.py is a bundled script. In Docker it lives at /app/lyric_fetch.py
# (entrypoint runs uvicorn with /app as the working dir, so it's importable);
# in a dev checkout it's at <repo>/scripts/lyric_fetch.py and not on sys.path
# unless we add it. Probe both before falling back to disabled.
HAVE_LYRIC_FETCH = False
lyric_fetch = None  # type: ignore

_LYRIC_CANDIDATES = []
if os.environ.get("QL_IN_CONTAINER"):
    _LYRIC_CANDIDATES.append(Path("/app"))
_LYRIC_CANDIDATES.append(Path(__file__).resolve().parents[3] / "scripts")
for _dir in _LYRIC_CANDIDATES:
    if (_dir / "lyric_fetch.py").exists():
        if str(_dir) not in _sys.path:
            _sys.path.insert(0, str(_dir))
        try:
            import lyric_fetch  # type: ignore  # noqa: F401
            HAVE_LYRIC_FETCH = True
            break
        except Exception:
            lyric_fetch = None  # type: ignore
            continue


# ── Lyric hook ────────────────────────────────────────────────────────────────

def _run_lyric_hook(album_dir):
    """Pre-import lyric fetch. Returns (counts, transient_signatures);
    the caller resolves signatures to post-import paths and persists
    them via _record_post_import_lyric_retry."""
    empty_result = ({}, [])
    if not cfg.LYRICS_ENABLED:
        return empty_result
    if not HAVE_LYRIC_FETCH:
        log.info(fmt(C.GRAY, "     (lyric_fetch unavailable; skipping lyrics hook)"))
        return empty_result
    if album_dir is None or not album_dir.exists():
        return empty_result
    try:
        flacs = sorted(album_dir.rglob("*.flac"))
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Lyric hook: couldn't list {album_dir}: {e}."))
        return empty_result
    if not flacs:
        return empty_result
    log.info(fmt(C.CYAN, f"  ⟳  Checking lyrics for {len(flacs)} track(s)…"))
    # lyric_fetch.save_state assumes its parent exists; ensure DATA_DIR is
    # created up-front so first-run (no other writer has touched DATA_DIR
    # yet) doesn't crash inside the lyric hook.
    try:
        cfg.LYRIC_FETCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        # Always embed here, regardless of cfg.LYRICS_FORMAT. This runs on
        # the *staging* dir before beets moves files: a .lrc written next
        # to a staging FLAC is orphaned the moment beets relocates+renames
        # the track (beets moves only the audio). Embedding puts the lyric
        # *inside* the FLAC so it travels through the move intact, then
        # write_post_import_sidecars() materialises the .lrc next to the
        # final renamed file when the user wants one. Net effect: the song
        # never lands in the library without its lyrics.
        counts = lyric_fetch.fetch_for_paths(
            flacs, log=log,
            providers=cfg.LYRICS_PROVIDERS or None,
            lyrics_format="embed",
            state_path=cfg.LYRIC_FETCH_STATE_FILE,
        )
    except Exception as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Lyric hook failed: {e}."))
        return empty_result
    synced       = counts.get("wrote-synced", 0)
    plain        = counts.get("wrote-plain", 0)
    nofnd        = counts.get("not-found", 0)
    already      = counts.get("already-synced", 0)
    # Files where every provider was unavailable when their turn came up;
    # lyric_fetch records these as status="transient" in its state file.
    # We surface the count and queue them for retry on next launch.
    unavailable  = counts.get("providers-unavailable", 0) + counts.get("transient", 0)

    if synced or plain or nofnd or unavailable:
        msg = (f"  ✓  lyrics: {synced} synced, {plain} plain, "
               f"{already} already-synced, {nofnd} not found")
        if unavailable:
            msg += fmt(C.YELLOW, f"  ({unavailable} provider-unavail → queued for retry)")
        log.info(fmt(C.GREEN, msg))

    # Capture tag signatures for transient files. The state lookup uses
    # staging paths (matches what lyric_fetch just wrote); the caller
    # will use these signatures to find the post-import paths after
    # beets imports.
    transient_signatures = []
    if unavailable:
        try:
            state = lyric_fetch.load_state(cfg.LYRIC_FETCH_STATE_FILE)
        except Exception as e:
            vlog(f"_run_lyric_hook: state load failed: {e}")
            state = {}
        from qobuz_librarian.integrations.rip import _flac_signature
        for fp in flacs:
            st = state.get(str(fp))
            if st is None or st.status != "transient":
                continue
            sig = _flac_signature(fp)
            if sig is not None:
                transient_signatures.append((sig, str(fp)))
    return counts, transient_signatures


def _resolve_signatures_to_paths(signatures, search_dirs):
    """Walk search_dirs and match each FLAC's tag signature.

    Returns the list of post-import paths that match. Used after beets
    has moved files out of staging, to find where transient-from-staging
    files landed. Stops as soon as every signature is matched. Unmatched
    signatures are silently dropped (file was renamed in a way we can't
    follow, or beets failed to import that specific track).

    `signatures` is the list returned by _run_lyric_hook (pairs of
    (sig, original_staging_path)); the staging path is informational
    and ignored here. `search_dirs` is an iterable of post-import
    album dirs — typically each queue item's resolved post_dir, plus
    the primary album dir for process_album."""
    if not signatures:
        return []
    sig_set = {sig for sig, _ in signatures}
    found = []
    seen = set()
    from qobuz_librarian.integrations.rip import _flac_signature
    for d in search_dirs:
        if not sig_set:
            break
        if d is None or not isinstance(d, Path):
            continue
        try:
            key = str(d.resolve())
        except OSError:
            key = str(d)
        if key in seen:
            continue
        seen.add(key)
        if not d.exists():
            continue
        try:
            for fp in d.rglob("*.flac"):
                if not fp.is_file():
                    continue
                sig = _flac_signature(fp)
                if sig is not None and sig in sig_set:
                    found.append(str(fp))
                    sig_set.discard(sig)
                    if not sig_set:
                        break
        except OSError as e:
            vlog(f"_resolve_signatures: rglob failed in {d}: {e}")
    return found


def write_post_import_sidecars(album_dirs):
    """Write .lrc sidecars next to imported FLACs when LYRICS_FORMAT
    requests them. Embedded tag is removed afterward for 'sidecar' mode,
    kept for 'both'."""
    if not cfg.LYRICS_ENABLED or not HAVE_LYRIC_FETCH:
        return
    # Only mutagen is needed to read the embedded tag and write the .lrc —
    # NOT syncedlyrics. (lyric_fetch.AVAILABLE conflates the two; gating on
    # it here would wrongly skip sidecars whenever the provider lib is
    # absent but mutagen is present.)
    if getattr(lyric_fetch, "FLAC", None) is None:
        return
    lyr_fmt = (cfg.LYRICS_FORMAT or "embed").strip().lower()
    if lyr_fmt not in ("sidecar", "both"):
        return
    strip_tag = lyr_fmt == "sidecar"
    seen = set()
    written = 0
    for d in album_dirs:
        if d is None or not isinstance(d, Path) or not d.exists():
            continue
        try:
            key = str(d.resolve())
        except OSError:
            key = str(d)
        if key in seen:
            continue
        seen.add(key)
        try:
            flacs = list(d.rglob("*.flac"))
        except OSError as e:
            vlog(f"sidecar: rglob failed in {d}: {e}")
            continue
        for fp in flacs:
            if not fp.is_file():
                continue
            try:
                f = lyric_fetch.FLAC(fp)
            except Exception as e:
                vlog(f"sidecar: FLAC open failed {fp}: {e}")
                continue
            content = lyric_fetch.get_existing_lyrics(f)
            if not content or not content.strip():
                continue
            try:
                lyric_fetch.write_sidecar(fp, content)
            except OSError as e:
                vlog(f"sidecar: write failed {fp}: {e}")
                continue
            written += 1
            if strip_tag:
                try:
                    if "lyrics" in f.tags:
                        del f.tags["lyrics"]
                        f.save()
                except Exception as e:
                    vlog(f"sidecar: tag strip failed {fp}: {e}")
    if written:
        log.info(fmt(C.GREEN,
            f"  ✓  lyrics: wrote {written} .lrc sidecar(s)"
            f"{' (embedded tag removed)' if strip_tag else ''}."))


def _record_post_import_lyric_retry(post_paths):
    """Update LYRIC_RETRY_FILE with post-import paths of transient files.

    Replaces the staging-keyed call to `_refresh_lyric_retry` from
    inside `_run_lyric_hook`. Existing manifest entries whose files
    no longer exist on disk are pruned (so stale staging paths from
    pre-fix runs get cleaned up on the first post-fix run). New
    post-import paths are merged in, deduped, and persisted.

    `_refresh_lyric_retry` itself is still used by
    `offer_resume_lyric_retry` after the resume's lyric_fetch run —
    by then state is keyed by post-import paths so it works correctly."""
    try:
        existing = load_lyric_retry()
    except Exception as e:
        vlog(f"_record_post_import_lyric_retry: load failed: {e}")
        existing = []
    fresh = [p for p in existing if Path(p).exists()]
    fresh = sorted(set(fresh) | set(post_paths))
    save_lyric_retry(fresh)


def _prune_lyric_state_orphans():
    """Drop staging-path keys from lyric_fetch's state file —
    they're orphaned the moment beets moves the file out of staging."""
    if not HAVE_LYRIC_FETCH:
        return
    try:
        state = lyric_fetch.load_state(cfg.LYRIC_FETCH_STATE_FILE)
    except Exception as e:
        vlog(f"_prune_lyric_state_orphans: load failed: {e}")
        return
    staging_prefix = str(cfg.STAGING_DIR)
    if not staging_prefix.endswith("/"):
        staging_prefix += "/"
    pruned = 0
    for k in list(state.keys()):
        if k.startswith(staging_prefix):
            del state[k]
            pruned += 1
    if pruned:
        try:
            lyric_fetch.save_state(state, cfg.LYRIC_FETCH_STATE_FILE)
            vlog(f"pruned {pruned} orphan lyric-state entry(ies) (staging-keyed)")
        except Exception as e:
            vlog(f"_prune_lyric_state_orphans: save failed: {e}")


# ── Lyric retry manifest ──────────────────────────────────────────────────────

def load_lyric_retry():
    """Return list of file paths queued for a lyric retry. Empty list if the
    manifest is missing, malformed, or unreadable."""
    if not cfg.LYRIC_RETRY_FILE.exists():
        return []
    try:
        payload = json.loads(cfg.LYRIC_RETRY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {cfg.LYRIC_RETRY_FILE.name} unreadable ({e}); ignoring."))
        return []
    if not isinstance(payload, dict):
        log.info(fmt(C.YELLOW,
            f"  ⚠  {cfg.LYRIC_RETRY_FILE.name} is malformed (not an object); "
            "ignoring."))
        return []
    if payload.get("version") != cfg.LYRIC_RETRY_VERSION:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {cfg.LYRIC_RETRY_FILE.name} version "
            f"{payload.get('version')!r} not supported by this build; ignoring."))
        return []
    files = payload.get("files") or []
    # Defensive — drop anything non-string in case of corruption.
    return [f for f in files if isinstance(f, str)]


def save_lyric_retry(paths):
    """Persist a deduplicated, sorted list of paths. If the list is empty,
    delete the file so a clean state has no leftover manifest."""
    paths = sorted({p for p in paths if isinstance(p, str) and p})
    if not paths:
        try:
            cfg.LYRIC_RETRY_FILE.unlink(missing_ok=True)
        except Exception as e:
            vlog(f"clear_lyric_retry: {e}")
        return
    try:
        cfg.LYRIC_RETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": cfg.LYRIC_RETRY_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "files": paths,
        }
        tmp = cfg.LYRIC_RETRY_FILE.with_suffix(cfg.LYRIC_RETRY_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, cfg.LYRIC_RETRY_FILE)
    except Exception as e:
        vlog(f"save_lyric_retry failed: {e}")


def _refresh_lyric_retry(flacs_just_processed):
    """After a lyric_fetch run, reconcile the manifest with current state.

    Reads lyric_fetch's state file directly. The manifest will end up containing
    exactly those files (from the union of prior-manifest entries and the
    just-processed flacs) whose state.status is currently "transient" — i.e.
    every provider was dead when their turn came. Resolved files (synced,
    plain, not_found, skipped, error) drop off naturally.
    """
    if not HAVE_LYRIC_FETCH:
        return
    try:
        # Late import to avoid pulling lyric helpers in at module load.
        state = lyric_fetch.load_state(cfg.LYRIC_FETCH_STATE_FILE)
    except Exception as e:
        vlog(f"_refresh_lyric_retry: couldn't load lyric state: {e}")
        return

    candidates = set(load_lyric_retry())
    candidates.update(str(p) for p in (flacs_just_processed or []))

    still_transient = []
    for p in candidates:
        st = state.get(p)
        if st is None:
            # File no longer tracked (maybe deleted, maybe state was reset);
            # drop it from the manifest.
            continue
        if st.status == "transient":
            still_transient.append(p)

    save_lyric_retry(still_transient)


def offer_resume_lyric_retry(args, token):
    """Startup hook. If files are queued for a lyric retry, prompt to retry
    now / keep for later / discard. Mirrors offer_resume_pending_queue."""
    paths = load_lyric_retry()
    if not paths:
        return False
    if not HAVE_LYRIC_FETCH:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {len(paths)} file(s) queued for lyric retry but "
            f"lyric_fetch is unavailable. Manifest preserved."))
        return False

    # Filter out files that have since vanished (manual move/delete).
    existing = [p for p in paths if Path(p).exists()]
    missing  = len(paths) - len(existing)
    if not existing:
        log.info(fmt(C.GRAY,
            f"  All {len(paths)} file(s) in {cfg.LYRIC_RETRY_FILE.name} "
            f"are missing on disk; clearing manifest."))
        save_lyric_retry([])
        return False

    print()
    log.info(fmt(C.BOLD + C.YELLOW,
        f"  ⚠  Lyric retry pending: {len(existing)} file(s) had providers "
        f"unavailable last run."))
    if missing:
        log.info(fmt(C.GRAY,
            f"     ({missing} more in the manifest no longer exist on disk; "
            f"will be dropped.)"))
    log.info(fmt(C.GRAY, f"     File: {cfg.LYRIC_RETRY_FILE}"))
    log.info(fmt(C.GRAY,
        "     These are NOT 'no lyrics exist' — those would already be "
        "marked"))
    log.info(fmt(C.GRAY,
        "     not-found and skipped here. These are files where every "
        "provider"))
    log.info(fmt(C.GRAY,
        "     was rate-limited or unreachable when their turn came up."))
    print()

    while True:
        try:
            ans = input(fmt(C.CYAN,
                "  Retry lyrics now? [Y]es / [k]eep for later / [d]iscard: "
            )).strip().lower()
        except EOFError:
            ans = ""
        if ans in ("", "y", "yes"):
            log.info(fmt(C.CYAN,
                f"\n  ⟳  Retrying lyrics on {len(existing)} file(s)…"))
            try:
                paths_obj = [Path(p) for p in existing]
                lyric_fetch.fetch_for_paths(
                    paths_obj, log=log,
                    providers=cfg.LYRICS_PROVIDERS or None,
                    lyrics_format=cfg.LYRICS_FORMAT,
                    state_path=cfg.LYRIC_FETCH_STATE_FILE,
                )
            except KeyboardInterrupt:
                log.info(fmt(C.YELLOW,
                    "\n  ⚠  Retry interrupted; manifest will be refreshed "
                    "from current state."))
            except Exception as e:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Retry failed: {e}; manifest preserved."))
                return False
            # Reconcile manifest against fresh state — resolved files drop,
            # still-transient stay.
            _refresh_lyric_retry(paths_obj)
            remaining = load_lyric_retry()
            if remaining:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  {len(remaining)} file(s) still need retry "
                    f"(providers still unhappy). Will prompt next launch."))
            else:
                log.info(fmt(C.GREEN, "  ✓  All retried files resolved."))
            return False  # fall through to menu
        if ans in ("k", "keep"):
            log.info(fmt(C.GRAY,
                "  Keeping the lyric retry manifest. It'll prompt again "
                "next launch."))
            return False
        if ans in ("d", "discard"):
            try:
                conf = input(fmt(C.YELLOW,
                    f"  Really discard {len(existing)} retry-queued file(s)? "
                    "Type DISCARD to confirm: ")).strip()
            except EOFError:
                conf = ""
            if conf == "DISCARD":
                save_lyric_retry([])
                log.info(fmt(C.GRAY, "  Lyric retry manifest cleared."))
            else:
                log.info(fmt(C.GRAY, "  Discard cancelled."))
            return False
        log.info(fmt(C.GRAY, "  Enter Y, k, or d."))
