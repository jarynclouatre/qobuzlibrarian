"""Album mode — single-album query, selection, and download.

"""
from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import (
    Aborted,
    AuthLost,
    CatalogMiss,
    QobuzError,
    friendly_qobuz_error,
)
from qobuz_librarian.api.search import get_album, search_albums
from qobuz_librarian.cli import parse_qobuz_url
from qobuz_librarian.library.catalog import compute_missing, find_existing_tracks
from qobuz_librarian.library.scanner import clear_scan_caches
from qobuz_librarian.modes.process import process_album
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.executor import _execute_download_queue
from qobuz_librarian.ui_cli.colors import C, banner, fmt
from qobuz_librarian.ui_cli.errors import EXIT_AUTH, EXIT_GENERAL, auth_lost_msg, die
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.ui_cli.prompts import (
    interactive_query,
    print_album_summary,
    prompt_album_selection,
)
from qobuz_librarian.ui_cli.sentinels import MORE, URL_QUERY


def resolve_album_from_args(args, token):
    """Album mode: resolve a single Qobuz album from CLI args or interactive prompt.
    Returns the album dict, or raises CatalogMiss/Aborted/AuthLost/QobuzError."""
    if args.query and "qobuz.com" in args.query[0]:
        parsed = parse_qobuz_url(args.query[0])
        if not parsed:
            die(fmt(C.RED,
                f"✗  Couldn't parse Qobuz URL: {args.query[0]}\n"
                f"   Supported formats:\n"
                f"     https://play.qobuz.com/album/<id>\n"
                f"     https://open.qobuz.com/album/<id>\n"
                f"     https://www.qobuz.com/<lang>/album/<slug>/<id>\n"),
                EXIT_GENERAL)
        kind, qid = parsed
        if kind == "track":
            from qobuz_librarian.cli import _compose_service_name
            _svc = _compose_service_name()
            die(fmt(C.RED,
                "✗  Track URL passed; Qobuz Librarian handles albums only.\n"
                "   For a single track use the bundled streamrip:\n"
                "     rip url <url>\n"
                f"   (in Docker: docker compose run --rm {_svc} "
                "rip url <url>)"),
                EXIT_GENERAL)
        log.info(fmt(C.GRAY, f"  ⟳  Fetching album {qid} …"))
        return get_album(qid, token)

    if len(args.query) >= 2:
        artist, album = args.query[0], " ".join(args.query[1:])
        query = f"{artist} {album}".strip()
    elif len(args.query) == 1:
        query = args.query[0].strip()
    else:
        sel = interactive_query()
        if sel is None:
            raise Aborted("user cancelled at album query")
        if sel[0] == URL_QUERY:
            parsed = parse_qobuz_url(sel[1])
            if parsed and parsed[0] == "track":
                die(fmt(C.RED,
                    "✗  That's a track URL — Qobuz Librarian works on albums.\n"
                    "   Paste the album URL instead, or use `rip url <track-url>` for a single track."),
                    EXIT_GENERAL)
            if not parsed:
                die(fmt(C.RED, "✗  Bad Qobuz URL — paste an album URL."),
                    EXIT_GENERAL)
            return get_album(parsed[1], token)
        artist, album = sel
        query = f"{artist} {album}".strip()

    if not query:
        die(fmt(C.RED, "✗  Empty query."), EXIT_GENERAL)

    search_limit = cfg.SEARCH_LIMIT
    first_search = True
    while True:
        if not first_search:
            log.info(fmt(C.GRAY, f"  ⟳  Loading more results (up to {search_limit}) …"))
        results = search_albums(query, token, limit=search_limit)
        first_search = False
        if not results:
            raise CatalogMiss(f"No Qobuz match for: {query}")
        can_load_more = (len(results) >= search_limit and search_limit < 50)
        chosen = prompt_album_selection(results, prefer_hires=args.prefer_hires,
                                        can_load_more=can_load_more)
        if chosen is None:
            raise Aborted("user cancelled at album selection")
        if chosen == MORE:
            search_limit = min(search_limit * 2, 50)
            continue
        break
    return get_album(chosen["id"], token)


def _interactive_album_action(album, args, token, album_queue, flush_queue):
    """Show the summary and run the [d]/[q]/[f]/[s] prompt for one album."""
    try:
        existing, album_dir = find_existing_tracks(album)
        qobuz_tracks = (album.get("tracks") or {}).get("items") or []
        missing, present = compute_missing(qobuz_tracks, existing)
        print_album_summary(album, missing, present, album_dir, args.force)

        if not missing and not args.force:
            log.info(fmt(C.GREEN, "  ✓  Already complete — nothing to download."))
            return
        if album_queue:
            log.info(fmt(C.GRAY,
                f"  ({len(album_queue)} album(s) already in queue — "
                "enter 'f' to download all)"))
        flush_opt = "  [f]lush queue" if album_queue else ""
        try:
            r = input(fmt(C.CYAN,
                f"  [d]ownload now (default)  [q]ueue for later"
                f"{flush_opt}  [s]kip: ")).strip().lower()
        except EOFError:
            r = "s"

        if r in ("q", "queue"):
            qi = _build_queue_item(
                album=album,
                album_dir=album_dir,
                label=(f"{(album.get('artist') or {}).get('name') or '?'}"
                       f" — {album.get('title') or '?'}"),
                missing=missing,
                present=present,
                upgrade_only=False,
                auto_upgrade=False,
            )
            album_queue.append(qi)
            log.info(fmt(C.CYAN,
                f"  ✓  Queued. ({len(album_queue)} album(s) in queue)"))
        elif r in ("f", "flush"):
            qi = _build_queue_item(
                album=album,
                album_dir=album_dir,
                label=(f"{(album.get('artist') or {}).get('name') or '?'}"
                       f" — {album.get('title') or '?'}"),
                missing=missing,
                present=present,
                upgrade_only=False,
                auto_upgrade=False,
            )
            album_queue.append(qi)
            flush_queue()
        elif r in ("s", "skip"):
            log.info(fmt(C.GRAY, "  Skipped."))
        else:
            try:
                process_album(album, args, allow_force=True, token=token)
            except AuthLost:
                die(fmt(C.RED, auth_lost_msg("mid-album")), EXIT_AUTH)
    except AuthLost:
        die(fmt(C.RED, auth_lost_msg("mid-album")), EXIT_AUTH)
    except QobuzError as e:
        log.info(fmt(C.RED, f"\n✗  Qobuz API error: {friendly_qobuz_error(e)}.\n"))


def run_album_mode(args, token, *, query_args=None, loop=False):
    """One pass of album mode: resolve, then process_album.

    With loop=True (interactive menu), repeats until the user hits q/blank
    at the search prompt. Offers a [d]ownload / [q]ueue / [s]kip prompt so
    multiple albums can be accumulated and batch-downloaded together.
    CatalogMiss/QobuzError are non-fatal in loop mode so the user can
    immediately try again with a different query.
    """
    album_queue = []
    interrupted = False

    def _flush_queue():
        if not album_queue:
            return
        banner(f"Executing queue — {len(album_queue)} album(s)", C.GREEN)
        _, drained = _execute_download_queue(album_queue, args, token)
        if not args.dry_run and not drained:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {len(album_queue)} album(s) couldn't be downloaded — "
                f"kept in the queue; retry or re-run to try again."))

    try:
        while True:
            clear_scan_caches()
            saved_query = args.query
            if query_args is not None:
                args.query = query_args
            try:
                album = resolve_album_from_args(args, token)
            except AuthLost:
                die(fmt(C.RED, auth_lost_msg("mid-album")), EXIT_AUTH)
            except CatalogMiss as e:
                log.info(fmt(C.YELLOW, f"\n⚠  {e}\n"))
                args.query = saved_query
                if not loop:
                    return
                continue
            except QobuzError as e:
                cleaned = friendly_qobuz_error(e)
                if cleaned.startswith("HTTP 404"):
                    log.info(fmt(C.RED,
                        "\n✗  No album with that id — check the URL or search by name.\n"))
                else:
                    log.info(fmt(C.RED, f"\n✗  Qobuz API error: {cleaned}.\n"))
                args.query = saved_query
                if not loop:
                    # One-shot invocation: a Qobuz API failure is fatal.
                    # Exit non-zero so cron/scripts can detect it (a plain
                    # return falls through to a 0 exit).
                    raise SystemExit(1)
                continue
            except Aborted as e:
                # Cancelling at the result picker (not the top-level query
                # prompt) should re-prompt in loop mode, NOT return — return
                # falls through to the finally block and flushes the queue.
                if loop and "selection" in str(e):
                    log.info(fmt(C.GRAY, "  Cancelled — back to album prompt."))
                    args.query = saved_query
                    continue
                log.info(fmt(C.GRAY, "  Cancelled."))
                args.query = saved_query
                return
            finally:
                args.query = saved_query

            if loop and not args.yes:
                _interactive_album_action(album, args, token, album_queue, _flush_queue)
            else:
                try:
                    process_album(album, args, allow_force=True, token=token)
                except AuthLost:
                    die(fmt(C.RED, auth_lost_msg("mid-album")), EXIT_AUTH)

            if not loop:
                return
    except KeyboardInterrupt:
        interrupted = True
        if album_queue:
            log.info(fmt(C.YELLOW,
                f"\n  ⚠  Interrupted with {len(album_queue)} album(s) queued — "
                "discarding queue (Ctrl+C means abort)."))
        raise
    finally:
        if not interrupted:
            _flush_queue()
