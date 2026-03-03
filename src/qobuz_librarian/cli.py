"""Entry point: argument parsing, pre-flight checks, and main dispatch.

Mode runner functions (run_album_mode, run_artist_mode, etc.) are
lazy-imported from qobuz_librarian.modes to keep startup fast.
"""
import argparse
import re
import subprocess

from qobuz_librarian import config as cfg
from qobuz_librarian import run_lock
from qobuz_librarian.api.auth import AuthLost, NoCredsError, load_qobuz_token
from qobuz_librarian.integrations.lyrics import _prune_lyric_state_orphans
from qobuz_librarian.integrations.rip import HAVE_MUTAGEN
from qobuz_librarian.library.backup import cleanup_old_upgrade_backups
from qobuz_librarian.quality.tiers import streamrip_quality_cap
from qobuz_librarian.queue.persistence import (
    offer_resume_pending_queue,
)
from qobuz_librarian.ui_cli.colors import C, banner, fmt, set_color_enabled
from qobuz_librarian.ui_cli.errors import (
    EXIT_AUTH,
    EXIT_CONFIG,
    EXIT_GENERAL,
    EXIT_LOCK_BUSY,
    die,
)
from qobuz_librarian.ui_cli.logging import attach_file_handler, log, set_quiet, set_verbose, vlog

# ── URL parsers ───────────────────────────────────────────────────────────────

_QOBUZ_PLAY_RE        = re.compile(r"(?:play|open)\.qobuz\.com/(album|track)/([A-Za-z0-9]+)")
_QOBUZ_STORE_ALBUM_RE = re.compile(r"qobuz\.com/[a-zA-Z-]+/album/[^/]+/([A-Za-z0-9]+)/?(?:[?#]|$)")
_QOBUZ_STORE_TRACK_RE = re.compile(r"qobuz\.com/[a-zA-Z-]+/track/[^/]+/([A-Za-z0-9]+)/?(?:[?#]|$)")


def parse_qobuz_url(url: str) -> tuple[str, str] | None:
    m = _QOBUZ_PLAY_RE.search(url)
    if m:
        return m.group(1), m.group(2)
    m = _QOBUZ_STORE_ALBUM_RE.search(url)
    if m:
        return "album", m.group(1)
    m = _QOBUZ_STORE_TRACK_RE.search(url)
    if m:
        return "track", m.group(1)
    return None


# ── Single-instance lock ──────────────────────────────────────────────────────

def _compose_service_name() -> str:
    """Best-effort docker compose service name for diagnostic hints.

    Docker sets HOSTNAME to the container's name when `container_name:` is
    set in compose, else to the 12-char container ID. Fall back to the
    generic name when we see an ID, so user-facing strings don't print a
    misleading hex blob.
    """
    import os
    h = os.environ.get("HOSTNAME", "").strip()
    if not h or (len(h) == 12 and all(c in "0123456789abcdef" for c in h)):
        return "qobuz-librarian"
    return h


def acquire_run_lock():
    """Acquire the single-writer run lock or exit."""
    try:
        return run_lock.acquire()
    except run_lock.LockBusy as busy:
        _svc = _compose_service_name()
        die(fmt(C.RED,
            f"\n✗  Another Qobuz Librarian run is in progress (pid {busy.pid}).\n"
            f"   Lock file: {cfg.LOCK_FILE}\n\n"
            f"   The web app is probably holding the lock. Either:\n"
            f"     1. In the web UI (http://<host>:{cfg.WEB_PORT}), open Settings → Mode\n"
            f"        and click 'Hand off to terminal', then re-run this command.\n"
            f"        (Or just use the web UI — every CLI mode is also a web action.)\n"
            f"     2. Stop the web container instead:  docker compose stop {_svc}\n"
            f"        then re-run, then `docker compose start {_svc}`.\n\n"
            f"   Only one writer can use /staging at a time.\n"),
            EXIT_LOCK_BUSY)


# ── Pre-flight checks ─────────────────────────────────────────────────────────

def _in_container() -> bool:
    import os
    return os.path.exists("/.dockerenv") or os.environ.get("QL_IN_CONTAINER") == "1"


def _missing_tool_hint(tool: str, install_hint: str) -> str:
    if _in_container():
        return (f"\n✗  `{tool}` not in PATH inside the container.\n"
                f"   This means the image is broken — rebuild with "
                f"`docker compose build --no-cache`.\n")
    return f"\n✗  `{tool}` not in PATH. {install_hint}\n"


def check_rip():
    try:
        r = subprocess.run(["rip", "--version"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            die(fmt(C.RED, _missing_tool_hint(
                "rip", "Try `pipx reinstall streamrip` or install streamrip "
                "(https://github.com/nathom/streamrip).")), EXIT_CONFIG)
    except FileNotFoundError:
        die(fmt(C.RED, _missing_tool_hint(
            "rip", "Install streamrip first: `pipx install streamrip` "
            "(https://github.com/nathom/streamrip).")), EXIT_CONFIG)


def check_ffprobe():
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, timeout=5)
    except FileNotFoundError:
        die(fmt(C.RED, _missing_tool_hint(
            "ffprobe", "Install ffmpeg via your package manager "
            "(e.g. `apt install ffmpeg`, `brew install ffmpeg`).")), EXIT_CONFIG)


# ── Argument parsing ──────────────────────────────────────────────────────────

class _ExitOneArgParser(argparse.ArgumentParser):
    """ArgumentParser that exits 1 (EXIT_GENERAL) on parse errors instead
    of argparse's default 2. Our documented exit-code contract reserves 2
    for EXIT_AUTH; without this override, a `--flag --conflict` typo
    surfaces with the same code as "token expired" and cron retry rules
    can't tell them apart.
    """

    def exit(self, status=0, message=None):
        if status == 2:
            status = EXIT_GENERAL
        return super().exit(status, message)


def parse_args():
    try:
        from importlib.metadata import version as _pkg_version
        _version = _pkg_version("qobuz-librarian")
    except Exception:
        _version = "0.2.2"
    p = _ExitOneArgParser(
        description="Qobuz Librarian — download albums/artists from Qobuz and "
                    "keep a library complete, only fetching what's missing. "
                    "Run with no arguments for an interactive menu.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (all require credentials — set them first):\n"
            "  qobuz-librarian                                  # interactive menu\n"
            "  qobuz-librarian https://open.qobuz.com/album/abc # one album by URL\n"
            "  qobuz-librarian \"radiohead in rainbows\"          # search and download\n"
            "  qobuz-librarian --artist \"Stars of the Lid\"      # fill artist gaps\n"
            "  qobuz-librarian --upgrade-walk --auto-safe       # unattended upgrade pass\n"
            "  qobuz-librarian --dry-run --artist Beatles       # preview without downloading\n\n"
            "Credentials: set them on the web UI Settings page first (browse to\n"
            "http://<host>:8666/settings on a fresh install), or via\n"
            "QOBUZ_USER_AUTH_TOKEN / QOBUZ_USER_ID env vars.\n\n"
            "Exit codes:\n"
            "  0   success\n"
            "  1   general failure (incl. interrupt)\n"
            "  2   auth: token invalid or missing\n"
            "  3   another writer holds the run lock\n"
            "  4   transient network/API error — retry later\n"
            "  64  config / required tool missing"
        ))
    p.add_argument("--version", action="version", version=f"qobuz-librarian {_version}")
    p.add_argument("query", nargs="*", help="search query or Qobuz album URL")
    p.add_argument("--artist",       metavar="NAME",
                   help="Run artist mode on NAME (skips interactive menu)")
    p.add_argument("--upgrade-walk", action="store_true",
                   help="Scan every artist for quality upgrades. Per-artist "
                        "confirm (enter=yes), auto-advance.")
    p.add_argument("--no-catalog",   action="store_true",
                   help="Artist mode: skip step 2 (don't show missing albums)")
    p.add_argument("--include-comps", action="store_true",
                   help="Artist mode: include compilation/various-artists releases in step 2")
    p.add_argument("--dry-run",      action="store_true", help="show plan, download nothing")
    p.add_argument("--no-import",    action="store_true",
                   help="download but skip beets import (run `beet import <staging>` afterward)")
    p.add_argument("--force",        action="store_true",
                   help="redownload everything (album mode only)")
    # Default comes from config (PREFER_HIRES, env/Settings overridable) so
    # CLI and web behave the same. --no-prefer-hires overrides per-run.
    p.add_argument("--prefer-hires", dest="prefer_hires",
                   action=argparse.BooleanOptionalAction,
                   default=cfg.PREFER_HIRES,
                   help="sort 24-bit / higher sample rate results first")
    p.add_argument("--yes",          action="store_true",
                   help="auto-confirm download/import prompts "
                        "(destructive prompts still ask)")
    p.add_argument("--verbose",      action="store_true", help="show detection details")
    # Default from config (CONSOLIDATE, env/Settings overridable).
    p.add_argument("--consolidate",  dest="consolidate",
                   action=argparse.BooleanOptionalAction,
                   default=cfg.CONSOLIDATE,
                   help="after import, scan sibling folders and offer to consolidate")
    # Passive auto-upgrade is OFF unless AUTO_UPGRADE_ENABLED is set or the
    # explicit Upgrade walk is run. --no-upgrade force-disables it for a run
    # even when the config flag is on. Backups are taken before any replace.
    p.add_argument("--no-upgrade",   action="store_true",
                   help="force-disable quality upgrades for this run (plain gap-fill)")
    # Unattended upgrade-walk gate.
    p.add_argument("--auto-safe",    action="store_true",
                   help="Auto-confirm only safe candidates (requires --upgrade-walk).")
    # Step 2 noise filter: hide/show short releases (singles, very small EPs).
    p.add_argument("--include-singles", action="store_true",
                   help=f"include releases with fewer than {cfg.MISSING_ALBUMS_MIN_TRACKS} tracks "
                        f"in step 2 of artist mode")
    p.add_argument("--no-color",     action="store_true", help="disable ANSI colors")
    p.add_argument("--quiet", "-q",  action="store_true",
                   help="suppress info-level output (errors still print to stderr)")
    p.add_argument("--reset-walk-seen", action="store_true",
                   help="delete the library-walk dedup files and exit "
                        "(so the next walk revisits every artist/album)")
    p.add_argument("--no-downsample",  dest="no_downsample",
                   action="store_true",
                   help="force-skip pre-import downsampling for this run "
                        "(only relevant when DOWNSAMPLE_HIRES_ENABLED is on)")
    # Legacy name kept so existing shell aliases / cron jobs don't break.
    p.add_argument("--no-compress", dest="no_compress",
                   action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--migrate-multi-artist", dest="migrate_multi_artist",
                   action=argparse.BooleanOptionalAction,
                   default=cfg.MIGRATE_MULTI_ARTIST,
                   help="after import, merge 'Primary, Other' folders into 'Primary'")
    args = p.parse_args()
    # Mirror the downsample-skip flag so either spelling resolves to both
    # attributes. Callers may read whichever name they're used to; the
    # behaviour is the same.
    if getattr(args, "no_compress", False) or getattr(args, "no_downsample", False):
        args.no_compress = True
        args.no_downsample = True
    else:
        args.no_compress = False
        args.no_downsample = False
    # Per-run override of cfg.AUTO_UPGRADE_ENABLED. Defaults to the global
    # so plain gap-fills behave the same as before; the explicit upgrade
    # walk flips this without mutating the cfg the web Settings page reads.
    args.auto_upgrade = cfg.AUTO_UPGRADE_ENABLED
    # Reject flag/mode combinations that would otherwise be silently
    # --auto-safe is only consulted by run_upgrade_walk_mode; the menu's
    # upgrade option uses the same code path, so don't reject it at parse
    # time — that would block menu users from running unattended.
    # --force re-downloads everything; it's an album-mode concept and is
    # ignored inside an artist scan / upgrade walk.
    if args.force and (args.artist or args.upgrade_walk):
        p.error("--force only applies to album mode (a query or Qobuz "
                "URL), not --artist or --upgrade-walk")
    # --include-singles only affects artist mode's missing-albums step.
    # Wrong with --upgrade-walk, or in album mode (a query without
    # --artist). Allowed with --artist or the interactive menu.
    if args.include_singles and (args.upgrade_walk
                                 or (args.query and not args.artist)):
        p.error("--include-singles only applies to artist mode")
    # --no-catalog skips artist mode's missing-albums step. The upgrade walk
    # has no such step and never reads the flag, so reject it there too rather
    # than accepting it silently. Bare --no-catalog stays valid for the menu.
    if args.no_catalog and not args.artist and (args.query or args.upgrade_walk):
        p.error("--no-catalog only applies to artist mode")
    # The upgrade walk sweeps the whole library; a query would be silently
    # dropped, so steer the user to --artist instead.
    if args.upgrade_walk and args.query:
        p.error("--upgrade-walk scans the whole library — drop the query, "
                "or use --artist NAME for one artist")
    # --artist dispatches before the positional query, so extra words after the
    # artist name would be silently dropped. Reject so the user picks one.
    if args.artist and args.query:
        p.error("--artist NAME scans that one artist — drop the extra words, "
                "or search them as an album without --artist")
    # --include-comps controls compilation filtering in artist mode.
    if args.include_comps and (args.upgrade_walk
                               or (args.query and not args.artist)):
        p.error("--include-comps only applies to artist mode")
    # --no-upgrade with --upgrade-walk is contradictory.
    if args.no_upgrade and args.upgrade_walk:
        p.error("--no-upgrade conflicts with --upgrade-walk")
    # --auto-safe only gates the unattended upgrade walk. With a query
    # (album mode) or --artist it does nothing — reject it instead of
    # accepting it silently. Bare --auto-safe stays valid: the interactive
    # menu's upgrade option reads it.
    if args.auto_safe and not args.upgrade_walk and (args.query or args.artist):
        p.error("--auto-safe only applies to --upgrade-walk")
    # --artist and --upgrade-walk are both whole-run modes; main() dispatches
    # --artist first, so the walk would be silently dropped. Run one at a time.
    if args.artist and args.upgrade_walk:
        p.error("--artist and --upgrade-walk are separate modes — run one at "
                "a time")
    # --reset-walk-seen clears the walk-history files and exits on its own;
    # pairing it with a mode or query silently skips that work.
    if args.reset_walk_seen and (args.artist or args.upgrade_walk or args.query):
        p.error("--reset-walk-seen runs on its own (it clears the walk history "
                "and exits) — run your artist/query/upgrade-walk separately")
    # An empty --artist (e.g. `--artist "$VAR"` with VAR unset) is falsy, so it
    # would silently fall through to the interactive menu instead of running
    # artist mode — confusing in a script. Reject it.
    if args.artist is not None and not args.artist.strip():
        p.error("--artist needs an artist name")
    return args


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Apply the web Settings page's persisted overrides before parse_args
    # reads cfg.* into the default flags. Without this, a user who toggles
    # MIGRATE_MULTI_ARTIST on in Settings has the change silently ignored
    # by CLI runs — the web process applies it on startup but the CLI never
    # reloads from disk.
    try:
        from qobuz_librarian.web import settings_store
        settings_store.load()
    except Exception:
        pass

    args = parse_args()
    set_verbose(args.verbose)
    set_quiet(args.quiet)
    attach_file_handler(cfg.APP_LOG_FILE, cfg.LOG_LEVEL)
    if args.quiet:
        set_color_enabled(False)

    if args.no_color:
        set_color_enabled(False)

    if args.reset_walk_seen:
        removed = []
        for f in (cfg.WALK_SEEN_FILE, cfg.ALBUM_WALK_SEEN_FILE):
            try:
                f.unlink()
                removed.append(str(f))
            except FileNotFoundError:
                pass
            except OSError as e:
                die(fmt(C.RED,
                    f"\n✗  Couldn't delete {f}: {e}\n"
                    "   Check the volume permissions / PUID-PGID — /data must be writable.\n"),
                    EXIT_GENERAL)
        if removed:
            log.info(fmt(C.GREEN, "  ✓  Cleared walk-seen state:"))
            for r in removed:
                log.info(fmt(C.GRAY, f"     {r}"))
        else:
            log.info(fmt(C.GRAY, "  No walk-seen state to clear."))
        return

    banner("Qobuz Librarian  —  search · artist · library · repair · upgrade")

    # Single-instance lock first — fail fast before doing any other work.
    # Hold the file handle for the lifetime of main() so the lock persists.
    _lockfile = acquire_run_lock()  # noqa: F841

    check_rip()
    check_ffprobe()

    if not cfg.MUSIC_ROOT.exists() or not cfg.MUSIC_ROOT.is_dir():
        die(fmt(C.RED,
            f"\n✗  MUSIC_ROOT missing or inaccessible: {cfg.MUSIC_ROOT}\n"
            "   Refusing to proceed.\n"), EXIT_CONFIG)

    from qobuz_librarian.api.auth import verify_streamrip_downloads_folder
    verify_streamrip_downloads_folder()
    if not HAVE_MUTAGEN:
        log.info(fmt(C.YELLOW, "  ⚠  mutagen not installed — falling back to filename-only detection."))
        if _in_container():
            log.info(fmt(C.GRAY,
                "     The bundled image installs mutagen by default — if it's missing here, "
                "rebuild with `docker compose build --no-cache`."))
        else:
            log.info(fmt(C.GRAY, "     Install: `pip install mutagen` (or via pipx)."))

    # Sweep upgrade-backup dir of anything older than retention window.
    # Cheap (just stat + rmtree on stale dirs); silent unless something happens.
    try:
        n_swept = cleanup_old_upgrade_backups()
        if n_swept:
            log.info(fmt(C.GRAY,
                f"  ⟳  Cleaned up {n_swept} old upgrade backup(s) "
                f"(>{cfg.UPGRADE_BACKUP_RETENTION_DAYS} days)."))
    except Exception as e:
        # Don't let backup-housekeeping fail the run.
        vlog(f"upgrade-backup cleanup error: {e}")
    # Prune orphan staging-path entries from lyric_fetch's
    # state file (created during pre-import lyric runs).
    try:
        _prune_lyric_state_orphans()
    except Exception as e:
        vlog(f"lyric-state prune error: {e}")
    try:
        user_id, token = load_qobuz_token()
    except NoCredsError:
        die(fmt(C.RED,
            "\n✗  No Qobuz credentials configured.\n"
            f"   Paste your user_auth_token on the Settings page (http://<host>:{cfg.WEB_PORT}/settings)\n"
            "   or set QOBUZ_USER_AUTH_TOKEN in your environment.\n"), EXIT_AUTH)
    # Env-provided creds authenticate our own API calls, but downloads
    # shell out to `rip`, which only reads the streamrip config. Mirror
    # env creds into it so the documented env-var setup actually downloads.
    from qobuz_librarian.api.auth import sync_streamrip_creds_from_env
    if sync_streamrip_creds_from_env() is False:
        log.info(fmt(C.YELLOW,
            "  ⚠  Couldn't write env credentials into the streamrip "
            f"config ({cfg.STREAMRIP_CONFIG}); downloads may fail."))
    vlog(f"user_id: {user_id}  •  music root: {cfg.MUSIC_ROOT}")
    if args.verbose:
        # compose.yaml lives on the host filesystem; inside the container
        # the bind mount isn't visible, so the "MISSING" line is just
        # noise. Note that fact instead of flagging a fake error.
        if _in_container():
            log.info(fmt(C.GRAY,
                f"  compose:    {cfg.COMPOSE_FILE}  "
                "(host-side; not visible from container)"))
        else:
            log.info(fmt(C.GRAY,
                f"  compose:    {cfg.COMPOSE_FILE}  "
                f"({'present' if cfg.COMPOSE_FILE.exists() else 'MISSING'})"))
        log.info(fmt(C.GRAY, f"  staging:    {cfg.STAGING_DIR}"))
        log.info(fmt(C.GRAY, f"  log file:   {cfg.FETCH_LOG_FILE}"))
        log.info(fmt(C.GRAY, f"  lock:       {cfg.LOCK_FILE}"))

    _capbd, _capsr = streamrip_quality_cap()
    vlog(f"streamrip quality cap: {_capbd}-bit/{_capsr/1000:g}kHz")
    # Token validation is lazy now — the first API call will raise
    # AuthLost on a bad token, which _entry catches and exits with
    # EXIT_AUTH. Modes that act purely on local files (e.g. consolidate
    # within an artist run that has nothing missing) skip the round-trip
    # entirely.

    # ── Decide the entry mode ─────────────────────────────────────────────────
    # Four entry paths:
    #   1. CLI positional args / URL  → album mode, single shot, no menu loop
    #   2. --artist NAME              → artist mode, single shot, no menu loop
    #   3. --upgrade-walk             → upgrade walk, single shot, no menu loop
    #   4. (no args)                  → interactive menu loop
    #
    # The single-shot paths still respect AuthLost / KeyboardInterrupt cleanly —
    # all caught at the bottom by main()'s wrapper.
    if args.artist:
        from qobuz_librarian.modes.artist import run_artist_mode
        run_artist_mode(args.artist, args, token)
        return

    if args.upgrade_walk:
        # AUTO_UPGRADE_ENABLED must stay False as the global default — it
        # controls passive upgrades during ordinary gap-fill walks. Set the
        # per-run flag so the replace path activates for this invocation
        # without mutating the module global the web Settings page reads.
        args.auto_upgrade = True
        from qobuz_librarian.modes.upgrade import run_upgrade_walk_mode
        run_upgrade_walk_mode(args, token)
        return

    if args.query:
        from qobuz_librarian.modes.album import run_album_mode
        run_album_mode(args, token)
        return

    # Crash-recovery: if a previous queueing run died with decisions still
    # in memory, we'd have left .qobuz_pending_queue.json on disk. Offer
    # to resume those before showing the menu so the user doesn't
    # accidentally start a new walk on top of pending work.
    offer_resume_pending_queue(args, token)

    # Same idea, but for files where every lyric provider was unavailable
    # last run. Surfaced separately because they're orthogonal: a flush
    # can succeed (queue cleared) while individual files in it ended up
    # shipped without lyrics.
    from qobuz_librarian.integrations.lyrics import offer_resume_lyric_retry
    offer_resume_lyric_retry(args, token)

    # Interactive menu loop
    from qobuz_librarian.ui_cli.menu import interactive_session_mode
    from qobuz_librarian.ui_cli.sentinels import Mode
    while True:
        mode = interactive_session_mode()
        if mode == Mode.QUIT:
            log.info(fmt(C.GRAY, "  Bye."))
            return
        if mode == Mode.ALBUM:
            # Loop inside album mode so the user can search album after album
            # without bouncing back to the top menu each time. q/blank at the
            # query prompt returns to the top menu.
            from qobuz_librarian.modes.album import run_album_mode
            run_album_mode(args, token, query_args=[], loop=True)
        elif mode == Mode.ARTIST:
            from qobuz_librarian.modes.artist import run_artist_mode
            from qobuz_librarian.ui_cli.prompts import prompt_artist_name
            while True:
                artist = prompt_artist_name()
                if artist is None:
                    log.info(fmt(C.GRAY, "  Cancelled."))
                    break
                run_artist_mode(artist, args, token)
        elif mode == Mode.WALK:
            from qobuz_librarian.modes.walk import run_library_walk_mode
            run_library_walk_mode(args, token)
        elif mode == Mode.WALK_QUEUE:
            from qobuz_librarian.modes.walk import run_walk_queued_mode
            run_walk_queued_mode(args, token)
        elif mode == Mode.ALBUM_WALK:
            from qobuz_librarian.modes.walk import run_album_walk_mode
            run_album_walk_mode(args, token)
        elif mode == Mode.ALBUM_REPAIR:
            from qobuz_librarian.modes.repair import run_album_repair_mode
            run_album_repair_mode(args, token, query_args=[], loop=True)
        elif mode == Mode.UPGRADE:
            # Explicit upgrade walk: the user chose this, so enable the
            # upgrade-replace path for its duration regardless of the
            # AUTO_UPGRADE_ENABLED default (which only governs passive
            # upgrades during ordinary gap-fill walks).
            from qobuz_librarian.modes.upgrade import run_upgrade_walk_mode
            saved = getattr(args, "auto_upgrade", cfg.AUTO_UPGRADE_ENABLED)
            args.auto_upgrade = True
            try:
                run_upgrade_walk_mode(args, token)
            finally:
                args.auto_upgrade = saved


def _check_staging_occupied():
    """Warn if STAGING_DIR has content left behind by a --no-import run or crash."""
    try:
        if not cfg.STAGING_DIR.exists():
            return
        subdirs = [d for d in cfg.STAGING_DIR.iterdir() if d.is_dir()]
        if subdirs:
            log.info(fmt(C.YELLOW,
                f"\n  ⚠  {len(subdirs)} album folder(s) remain in "
                f"{cfg.STAGING_DIR} — run `beet import {cfg.STAGING_DIR}` "
                "to finish importing."))
    except OSError:
        pass


def _maybe_drop_privileges():
    """Re-exec under gosu to PUID/PGID when started as root.

    The entrypoint drops PID 1 to PUID/PGID, but `docker exec ... qobuz-librarian`
    bypasses the entrypoint and runs as root, so files the CLI downloads land
    root-owned while web-created ones match PUID — and the web process (running
    as PUID) then can't repair/upgrade a CLI-fetched album. Dropping here keeps
    both writers consistent. No-op when not root, when PUID/PGID are unset or
    non-numeric, or when gosu isn't on PATH.
    """
    import os
    import shutil
    import sys
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    puid = (os.environ.get("PUID") or "").strip()
    pgid = (os.environ.get("PGID") or "").strip()
    if not puid and not pgid:
        return
    puid = puid or "1000"
    pgid = pgid or "1000"
    if not (puid.isdigit() and pgid.isdigit()):
        return
    gosu = shutil.which("gosu")
    if not gosu:
        return
    home = os.environ.get("APP_HOME", "/tmp")
    os.execvp(gosu, [gosu, f"{puid}:{pgid}", "env", f"HOME={home}",
                     sys.argv[0], *sys.argv[1:]])


def _entry():
    """Console-script entry point. Centralizes interrupt and AuthLost
    handling so every mode dispatch in main() can let them propagate."""
    _maybe_drop_privileges()
    try:
        try:
            main()
        except KeyboardInterrupt:
            die(fmt(C.GRAY, "\n  Interrupted."), EXIT_GENERAL)
        except AuthLost:
            die(fmt(C.RED,
                "\n✗  Auth lost. Re-authenticate: Settings page in the web UI, "
                "or set QOBUZ_USER_AUTH_TOKEN in your environment.\n"), EXIT_AUTH)
    finally:
        _check_staging_occupied()


if __name__ == "__main__":
    _entry()
