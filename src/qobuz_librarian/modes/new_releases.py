"""CLI new-release check — surface what's been added to Qobuz since the last
check across all library artists.

Uses the same engine as the web dashboard's auto-check (the cheap one-call-per-
artist diff against the last-seen catalog ids), just printed to the terminal
instead of routed through a job's candidate list. Cancellable with Ctrl-C.
``--dry-run`` skips advancing the baseline so a user can preview without losing
the chance to see the same releases again on the next run.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    AuthLost,
    NoCredsError,
    QobuzUnavailable,
    load_qobuz_token,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import new_releases as new_releases_mod
from qobuz_librarian.library.discovery import (
    DiscoveryOpts,
    find_new_releases_for_artist,
    flush_resolve_cache,
)
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_library_artists,
)
from qobuz_librarian.ui_cli.colors import C, fmt, section
from qobuz_librarian.ui_cli.errors import EXIT_AUTH, die, plural
from qobuz_librarian.ui_cli.logging import log


def run_check_new_releases_mode(args):
    """Walk library artists and report what's new on Qobuz since the last check.

    Mirrors the engine the web auto-check uses, so a run advances the same
    'seen' baseline and the dashboard's age line picks it up. First run on a
    library records the baseline and surfaces nothing — same first-touch
    contract as the web.
    """
    try:
        _user_id, token = load_qobuz_token()
    except NoCredsError:
        die(fmt(C.RED,
            "✗  No Qobuz credentials configured.\n"
            f"   Paste your user_auth_token on the Settings page "
            f"(http://<host>:{cfg.WEB_PORT}/settings)\n"
            "   or set QOBUZ_USER_AUTH_TOKEN in your environment.\n"),
            EXIT_AUTH)

    clear_scan_caches()
    artists = list_library_artists()
    if not artists:
        log.info(fmt(C.YELLOW, "No artist folders found under MUSIC_ROOT."))
        return

    state = new_releases_mod.load()
    seen = state.get("seen") or {}
    hidden = hidden_mod.load()
    opts = DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES)

    section(f"New-release check — {plural(len(artists), 'artist')}")
    if not seen:
        log.info(fmt(C.GRAY,
            "  First check on this library — recording the current catalog "
            "as the baseline. Nothing surfaces this run; later runs diff "
            "against this snapshot."))

    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    current_seen = {}
    total_new = 0
    artists_with_news = 0

    try:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="newrel") as ex:
            futures = {ex.submit(find_new_releases_for_artist, ad.name,
                                 token=token, opts=opts, seen_by_id=seen,
                                 hidden=hidden, single_store=hidden,
                                 artist_dir=ad): ad
                       for ad in artists}
            for fut in as_completed(futures):
                ad = futures[fut]
                try:
                    result = fut.result()
                except (AuthLost, QobuzUnavailable):
                    for f in futures:
                        f.cancel()
                    raise
                except Exception as e:
                    log.info(fmt(C.GRAY, f"    skipped {ad.name}: {e}"))
                    continue
                if result.artist_id:
                    current_seen[result.artist_id] = result.current_ids
                if result.new_gaps:
                    artists_with_news += 1
                    log.info(fmt(C.GREEN,
                        f"  {result.artist_name} — "
                        f"{plural(len(result.new_gaps), 'new release')}"))
                    for gap in result.new_gaps:
                        a = gap.qobuz_album
                        title = a.get("title") or "?"
                        year = (str(a.get("release_date_original") or "")[:4]
                                or "?")
                        log.info(fmt(C.WHITE, f"    • {title} ({year})"))
                    total_new += len(result.new_gaps)
    except KeyboardInterrupt:
        log.info(fmt(C.YELLOW, "\n  ⚠  Cancelled — not recording this run."))
        raise

    flush_resolve_cache()
    log.info("")
    if total_new:
        log.info(fmt(C.BOLD + C.WHITE,
            f"  ✓  {plural(total_new, 'new release')} across "
            f"{plural(artists_with_news, 'artist')}."))
        log.info(fmt(C.GRAY,
            "  Run the web library scan or the per-artist scan to queue them."))
    elif seen:
        log.info(fmt(C.GRAY, "  · Nothing new found."))
    else:
        log.info(fmt(C.GRAY, "  · Baseline recorded."))

    if not args.dry_run:
        # Merge over the existing baseline so an artist that errored doesn't
        # lose its prior 'seen' set — matches the web flow's semantics.
        new_releases_mod.mark_run({**seen, **current_seen}, complete=True)
