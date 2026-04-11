"""Walk modes — library walk, walk+queue, and album fill walk."""
import os
import re
import sys

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_librarian.library.tags import VA_NORMALIZED, normalize
from qobuz_librarian.modes.artist import (
    resolve_artist_dir,
    run_artist_gap_fill,
    run_artist_missing_albums,
)
from qobuz_librarian.queue.executor import _execute_download_queue
from qobuz_librarian.queue.persistence import clear_pending_queue, save_pending_queue
from qobuz_librarian.ui_cli.colors import C, banner, fmt, truncate
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import _flush_stdin, confirm

# ── Artist walk seen file ─────────────────────────────────────────────────────

def load_walk_seen():
    """Load set of normalized artist names already decided in walk mode."""
    if not cfg.WALK_SEEN_FILE.exists():
        return set()
    seen = set()
    try:
        with open(cfg.WALK_SEEN_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                n = normalize(s)
                if n:
                    seen.add(n)
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Couldn't read {cfg.WALK_SEEN_FILE.name}: {e}."))
    return seen


def _atomic_append(path, header_lines, entry):
    """Read-modify-write via tmp+os.replace.

    Plain "a" mode leaves a truncated line on the disk if the process dies
    mid-write; the next reader silently drops the partial line and the
    entry is lost. Writing the full intended content to a sibling tmp and
    renaming it eliminates the partial-write window.
    """
    existing = path.read_bytes() if path.exists() else b""
    content = bytearray()
    if not existing and header_lines:
        for line in header_lines:
            content.extend(line.encode("utf-8"))
    content.extend(existing)
    content.extend(entry.encode("utf-8"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(bytes(content))
    os.replace(tmp, path)


def record_walk_seen(artist_name):
    """Append a decided artist to the walk seen file (creates with header).

    Skip if already recorded (normalized) so the file
    doesn't grow unbounded across re-walks of the same library.
    """
    try:
        cfg.WALK_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        if normalize(artist_name) in load_walk_seen():
            return
        _atomic_append(
            cfg.WALK_SEEN_FILE,
            header_lines=[
                "# Library walk decisions — artists you've answered y/N for during the library walk.\n",
                "# Skipped on future walks so you don't loop the same names.\n",
                "# To revisit an artist, delete its line below (or delete this whole file).\n",
                "# Comments start with #. One artist per line.\n",
                "\n",
            ],
            entry=artist_name + "\n",
        )
    except OSError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  Couldn't write {cfg.WALK_SEEN_FILE.name}: {e}."))


# ── Album-fill-walk seen file ─────────────────────────────────────────────

def _album_seen_key(artist_name, album_name):
    """Normalized 'artist::album' key for the album-walk seen file.

    Uses the same normalize() that artist-level matching uses, so 'AC/DC' /
    'AC_DC' / 'ac dc' all map to one key. The :: separator is illegal in
    normalized output (normalize strips punctuation), so it can't collide
    with a real artist or album name.
    """
    return f"{normalize(artist_name)}::{normalize(album_name)}"


def load_album_walk_seen():
    """Load set of normalized 'artist::album' keys already decided during the album fill walk."""
    if not cfg.ALBUM_WALK_SEEN_FILE.exists():
        return set()
    seen = set()
    try:
        with open(cfg.ALBUM_WALK_SEEN_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if " | " not in s:
                    continue
                artist, album = s.split(" | ", 1)
                key = _album_seen_key(artist, album)
                if key:
                    seen.add(key)
    except OSError as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't read {cfg.ALBUM_WALK_SEEN_FILE.name}: {e}."))
    return seen


def record_album_walk_seen(artist_name, album_name):
    """Append a decided 'Artist | Album' entry to the album-walk seen file."""
    try:
        cfg.ALBUM_WALK_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _album_seen_key(artist_name, album_name) in load_album_walk_seen():
            return
        _atomic_append(
            cfg.ALBUM_WALK_SEEN_FILE,
            header_lines=[
                "# Album fill walk decisions — albums you've answered "
                "y/N for during the album fill walk.\n",
                "# Format: Artist | Album, one per line.\n",
                "# To revisit an album, delete its line below "
                "(or delete this whole file).\n",
                "# Comments start with #.\n",
                "\n",
            ],
            entry=f"{artist_name} | {album_name}\n",
        )
    except OSError as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't write {cfg.ALBUM_WALK_SEEN_FILE.name}: {e}."))


# ── Album fill walk ───────────────────────────────────────────────────

def run_album_walk_mode(args, token):
    """Album fill walk: scan every album under every artist and
    prompt only on incomplete ones."""
    banner("Album gaps — fill missing tracks in albums you already own")

    all_artists = list_library_artists()
    if not all_artists:
        log.info(fmt(C.YELLOW, "  ⚠  No artist directories found in library."))
        return

    vlog(f"  {plural(len(all_artists), 'artist')} in library.")
    try:
        flt = input(fmt(C.CYAN,
            "  Filter artists (substring, case-insensitive; blank = all): "
        )).strip().lower()
    except EOFError:
        flt = ""
    artists = all_artists
    if flt:
        artists = [a for a in all_artists if flt in a.name.lower()]
        log.info(fmt(C.GRAY, f"  {len(artists)} artist(s) match {flt!r}."))
        if not artists:
            return

    seen = load_album_walk_seen()
    if seen:
        vlog(f"  {len(seen)} album decision(s) loaded from "
             f"{cfg.ALBUM_WALK_SEEN_FILE.name}.")
    log.info(fmt(C.GRAY,
        "  Per album: y=queue, enter/N=skip, d=download queue now, s=stop."))
    log.info(fmt(C.GRAY,
        "  Already-complete albums and previously-decided ones are skipped "
        "silently."))
    log.info(fmt(C.GRAY,
        "  --yes auto-y's anything not yet decided (useful for crash "
        "recovery)."))
    print()

    saved_consolidate = args.consolidate
    args.consolidate = False

    shared_queue = []

    def _save_now():
        save_pending_queue(shared_queue, mode="album_walk")

    def _flush_queue():
        if not shared_queue:
            log.info(fmt(C.GRAY, "    Queue is empty — nothing to flush."))
            return
        save_pending_queue(shared_queue, mode="album_walk")
        log.info(fmt(C.CYAN,
            f"\n  ⟳  Flushing queue ({len(shared_queue)} album(s))…"))
        _, drained = _execute_download_queue(
            shared_queue, args, token, on_progress=_save_now)
        if args.dry_run:
            return
        if drained:
            clear_pending_queue()
        else:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {len(shared_queue)} album(s) couldn't be downloaded — "
                f"kept in the queue to retry on the next launch."))

    n_artists_scanned = 0
    n_albums_complete = 0
    n_albums_prompted = 0
    interrupted = False

    try:
        for i, artist_dir in enumerate(artists, 1):
            artist_query = re.sub(r"\s+", " ",
                                  artist_dir.name.replace("_", " ")).strip()
            if normalize(artist_query) in VA_NORMALIZED:
                vlog(f"  [{i}/{len(artists)}] Skipping '{artist_query}' "
                     "(not a real artist).")
                continue

            try:
                _albs = list_artist_album_dirs(artist_dir)
            except Exception:
                _albs = []
            if _albs and all(
                _album_seen_key(artist_query, ad.name) in seen
                for ad in _albs
            ):
                vlog(f"  [{i}/{len(artists)}] {artist_query}: "
                     "all albums already decided — skipping.")
                continue

            print()
            qhint = (f" [queue: {len(shared_queue)}]"
                     if shared_queue else "")
            log.info(fmt(C.BOLD + C.CYAN,
                f"  [{i}/{len(artists)}] {artist_query}{qhint}"))

            _seen_pred = (
                lambda ad, _aq=artist_query:
                _album_seen_key(_aq, ad.name) in seen
            )

            try:
                clear_scan_caches()
                gap_fill_result = run_artist_gap_fill(
                    artist_query, artist_dir, args, token,
                    shared_queue=shared_queue,
                    flush_callback=_flush_queue,
                    skip_predicate=_seen_pred,
                    save_callback=_save_now,
                )
                _RECORDABLE_RESULTS = {
                    "already_complete", "user_skipped", "user_stopped",
                    "no_qobuz_match", "no_tracks", "false_match",
                    "predicted_path_mismatch",
                }
                for r in (gap_fill_result[0] if gap_fill_result else []):
                    _ad = r.get("dir")
                    _result = r.get("result")
                    if _ad is None or _result == "dry_run":
                        continue
                    if _result in _RECORDABLE_RESULTS:
                        record_album_walk_seen(artist_query, _ad.name)
                        seen.add(_album_seen_key(artist_query, _ad.name))
                    if _result == "already_complete":
                        n_albums_complete += 1
                    else:
                        n_albums_prompted += 1
                n_artists_scanned += 1
            except KeyboardInterrupt:
                log.info(fmt(C.GRAY,
                    "\n  Walk interrupted — queue is persisted to "
                    f"{cfg.PENDING_QUEUE_FILE.name}; resume next launch."))
                interrupted = True
                break
            except AuthLost:
                raise
    finally:
        args.consolidate = saved_consolidate

    if shared_queue and not interrupted:
        try:
            _q = input(fmt(C.CYAN,
                f"\n  Walk done. {len(shared_queue)} album(s) still queued."
                " Download now? [Y/n]: ")).strip().lower()
        except EOFError:
            _q = "y"
        if _q in ("", "y", "yes"):
            try:
                _flush_queue()
            except KeyboardInterrupt:
                log.info(fmt(C.YELLOW,
                    "\n  ⚠  Final flush interrupted; queue persisted to "
                    f"{cfg.PENDING_QUEUE_FILE.name} for resume."))
        else:
            save_pending_queue(shared_queue, mode="album_walk")
            log.info(fmt(C.GRAY,
                f"  Queue retained ({len(shared_queue)} album(s)) — "
                f"persisted to {cfg.PENDING_QUEUE_FILE.name} for next launch."))

    print()
    log.info(fmt(C.GREEN,
        f"  ✓ Album walk complete. "
        f"Artists scanned: {n_artists_scanned} · "
        f"Albums skipped (complete): {n_albums_complete} · "
        f"Albums prompted: {n_albums_prompted}"))


# ── Library walk ──────────────────────────────────────────────────────

def run_walk_queued_mode(args, token):
    """Walk artists, accumulate decisions across artists, flush on demand."""
    banner("Library walk — scan artists, queue, download when you choose")

    all_artists = list_library_artists()
    if not all_artists:
        log.info(fmt(C.YELLOW, "  ⚠  No artist directories found in library."))
        return

    seen = load_walk_seen()
    if seen:
        n_before = len(all_artists)
        all_artists = [a for a in all_artists if normalize(a.name) not in seen]
        n_hidden = n_before - len(all_artists)
        if n_hidden:
            vlog(f"  Hiding {n_hidden} previously-decided artist(s).")
    if not all_artists:
        log.info(fmt(C.GREEN, "  ✓  All artists already decided."))
        return

    vlog(f"  {len(all_artists)} artist(s) to walk.")
    try:
        flt = input(fmt(C.CYAN,
            "  Filter (substring, case-insensitive; blank = all): "
        )).strip().lower()
    except EOFError:
        flt = ""
    artists = all_artists
    if flt:
        artists = [a for a in all_artists if flt in a.name.lower()]
        log.info(fmt(C.GRAY, f"  {len(artists)} artist(s) match {flt!r}."))
        if not artists:
            return

    log.info(fmt(C.GRAY,
        "  Per artist: y=scan+queue, enter/n=skip, p=process queue,"))
    log.info(fmt(C.GRAY,
        "             s/q=stop walk, f <substring>=filter rest."))
    print()

    shared_queue = []
    saved_consolidate = args.consolidate
    args.consolidate = False

    def _save_now():
        save_pending_queue(shared_queue, mode="walk_queue")

    def _flush_queue():
        if not shared_queue:
            log.info(fmt(C.GRAY, "  Queue is empty — nothing to process."))
            return 0
        save_pending_queue(shared_queue, mode="walk_queue")
        log.info(fmt(C.CYAN,
            f"\n  ⟳  Flushing queue ({len(shared_queue)} album(s))…"))
        results, drained = _execute_download_queue(
            shared_queue, args, token, on_progress=_save_now)
        if not args.dry_run:
            if drained:
                clear_pending_queue()
            else:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  {len(shared_queue)} album(s) couldn't be downloaded "
                    f"— kept in the queue to retry on the next launch."))
        return sum(1 for r in results
                   if r.get("result") in ("downloaded", "partial"))

    n_scanned = 0
    n_skipped = 0
    i = 0

    try:
        while i < len(artists):
            d = artists[i]
            _flush_stdin()
            qsize = len(shared_queue)
            qhint = f" [queue: {qsize}]" if qsize else ""
            try:
                r = input(fmt(C.CYAN,
                    f"  [{i + 1}/{len(artists)}] {truncate(d.name, 50)}"
                    f"{qhint} — scan? [y/N/p/s/f]: "
                )).strip()
            except EOFError:
                log.info(fmt(C.GRAY, "\n  EOF — stopping walk."))
                break
            rl = r.lower()

            if rl in ("s", "q", "quit"):
                log.info(fmt(C.GRAY, "  Stopping walk."))
                break
            if rl == "p":
                _flush_queue()
                continue
            if rl == "f":
                log.info(fmt(C.GRAY,
                    "  Filter syntax: 'f <substring>' (e.g. 'f mar')"))
                continue
            if rl.startswith("f "):
                sub = r[2:].strip().lower()
                if sub:
                    remaining = [a for a in artists[i:]
                                 if sub in a.name.lower()]
                    if remaining:
                        artists = artists[:i] + remaining
                        log.info(fmt(C.GRAY,
                            f"  Filtered to {len(remaining)} artist(s)."))
                    else:
                        log.info(fmt(C.YELLOW,
                            f"  No remaining artists match {sub!r}."))
                continue

            decided = True
            if rl in ("y", "yes"):
                artist_query = re.sub(r"\s+", " ",
                                      d.name.replace("_", " ")).strip()
                artist_dir_resolved = resolve_artist_dir(artist_query)
                if artist_dir_resolved is None:
                    log.info(fmt(C.YELLOW,
                        "  ⚠  No matching artist directory; skipping."))
                elif normalize(artist_query) in VA_NORMALIZED:
                    log.info(fmt(C.YELLOW,
                        "  ⚠  'Various Artists' isn't a real artist; skipping."))
                else:
                    print()
                    banner(f"Scanning — {artist_query}")
                    try:
                        clear_scan_caches()
                        gap_fill_result = run_artist_gap_fill(
                            artist_query, artist_dir_resolved,
                            args, token, shared_queue=shared_queue,
                            save_callback=_save_now,
                        )
                        (_, owned_bare_titles, seen_album_ids,
                         seed_id, prefetched_catalog) = gap_fill_result
                        if not args.no_catalog:
                            run_artist_missing_albums(
                                artist_query, owned_bare_titles,
                                args, token,
                                seed_artist_id=seed_id,
                                seen_album_ids=seen_album_ids,
                                prefetched_catalog=prefetched_catalog,
                                shared_queue=shared_queue,
                            )
                            _save_now()
                    except KeyboardInterrupt:
                        log.info(fmt(C.GRAY,
                            "\n  Artist scan interrupted — queue persisted to "
                            f"{cfg.PENDING_QUEUE_FILE.name}; resume next launch."))
                        decided = False

                n_scanned += 1

                if shared_queue:
                    print()
                    if confirm(
                            f"  Process queue now ({len(shared_queue)} album(s))?",
                            default_yes=False, auto_yes=args.yes):
                        _flush_queue()
                print()
            elif rl in ("", "n", "no"):
                n_skipped += 1
            else:
                log.info(fmt(C.YELLOW,
                    f"  Unrecognized: {r!r}. Use y/N/p/s/f."))
                continue

            if decided:
                record_walk_seen(d.name)
            i += 1
    finally:
        args.consolidate = saved_consolidate
        if shared_queue and isinstance(sys.exc_info()[1], AuthLost):
            # Unwinding on a lost token: a flush would only re-raise over the
            # original auth error, so keep the queue for next launch instead.
            save_pending_queue(shared_queue, mode="walk_queue")
            log.info(fmt(C.GRAY,
                f"  Queue retained — persisted to "
                f"{cfg.PENDING_QUEUE_FILE.name} for next launch."))
        elif shared_queue:
            print()
            log.info(fmt(C.YELLOW,
                f"  {len(shared_queue)} album(s) still queued."))
            try:
                if confirm("  Process them before exiting?",
                           default_yes=True, auto_yes=args.yes):
                    try:
                        _flush_queue()
                    except KeyboardInterrupt:
                        log.info(fmt(C.YELLOW,
                            "\n  ⚠  Final flush interrupted; queue persisted "
                            f"to {cfg.PENDING_QUEUE_FILE.name} for resume."))
                else:
                    save_pending_queue(shared_queue, mode="walk_queue")
                    log.info(fmt(C.GRAY,
                        f"  Queue retained — persisted to "
                        f"{cfg.PENDING_QUEUE_FILE.name} for next launch."))
            except KeyboardInterrupt:
                save_pending_queue(shared_queue, mode="walk_queue")
                log.info(fmt(C.GRAY,
                    f"\n  Interrupted — queue persisted to "
                    f"{cfg.PENDING_QUEUE_FILE.name} for next launch."))

    print()
    log.info(fmt(C.GREEN,
        f"  ✓ Walk done. Scanned {n_scanned}, skipped {n_skipped}."))
