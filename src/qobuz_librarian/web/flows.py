"""Scan / execute logic behind the web Artist and Library flows.

These wrap the same engine the CLI uses (catalog matching, gap detection,
process_album) but without any terminal prompts — a scan attaches review
candidates to the job, and execution runs over the candidates the user kept.
"""
import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost
from qobuz_librarian.api.search import (
    get_album,
    get_artist_albums,
    search_artists,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.catalog import (
    album_quality_label,
    album_year,
    compute_missing,
    dedup_album_versions,
    filter_compilation_albums,
    filter_short_releases,
    find_existing_tracks,
    find_qobuz_album_for_dir,
)
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_librarian.library.tags import VA_NORMALIZED, normalize, similarity
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log


def build_args():
    """Namespace of CLI flags used by process_album and the artist/walk runners.

    `consolidate` is forced False: the web has no confirm() UI, so letting
    the engine scan for siblings it can't act on would waste time.
    """
    return argparse.Namespace(
        force=False, yes=True, dry_run=False, no_import=False,
        no_upgrade=False, no_downsample=False, no_compress=False,
        prefer_hires=cfg.PREFER_HIRES,
        consolidate=False,
        migrate_multi_artist=cfg.MIGRATE_MULTI_ARTIST,
        include_comps=False,
        include_singles=False,
        no_catalog=False,
        auto_safe=False,
        # auto_upgrade is request-scoped (the explicit Upgrade flow flips it
        # to True for one run) so the Settings page can keep reading the
        # underlying cfg.AUTO_UPGRADE_ENABLED without a mid-job racy flip.
        auto_upgrade=cfg.AUTO_UPGRADE_ENABLED,
        verbose=False,
    )


_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def _strip_leading_article(name):
    """Drop a leading 'the/a/an ' so 'The Beatles' compares equal to a bare
    'Beatles'. Left unchanged if stripping would empty it."""
    return _LEADING_ARTICLE_RE.sub("", name or "", count=1) or name


# Persistent artist-resolution cache (folder name → matched Qobuz artist).
# Resolution is deterministic and artist IDs are stable, so a re-scan skips the
# artist-search call for every artist already matched — the slow half of a
# library scan. Misses are NOT cached (re-tried each scan, in case the artist
# later appears on Qobuz). Bump the version if resolve_artist's matching logic
# changes so stale matches drop; delete the file to force a full re-resolve.
_RESOLVE_CACHE_VERSION = 1
_resolve_cache = None
_resolve_cache_dirty = False


def _load_resolve_cache() -> dict:
    global _resolve_cache
    if _resolve_cache is None:
        _resolve_cache = {}
        try:
            raw = json.loads((cfg.DATA_DIR / ".artist_resolve_cache.json")
                             .read_text(encoding="utf-8"))
            if raw.get("version") == _RESOLVE_CACHE_VERSION:
                _resolve_cache = raw.get("entries") or {}
        except (OSError, ValueError):
            pass
    return _resolve_cache


def flush_resolve_cache():
    """Persist the resolution cache to disk, only if it gained entries."""
    global _resolve_cache_dirty
    if not _resolve_cache_dirty or _resolve_cache is None:
        return
    import os
    import tempfile
    path = cfg.DATA_DIR / ".artist_resolve_cache.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": _RESOLVE_CACHE_VERSION,
                              "entries": _resolve_cache})
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".arcache.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
        _resolve_cache_dirty = False
    except OSError:
        pass


def resolve_artist(query, token):
    """Return (artist_id, artist_name) for the best match, or (None, None).

    Qobuz lists the canonical artist ('The Beatles') next to bare-name twins
    ('Beatles') that aggregate covers, interviews and bootlegs. A raw string
    match favours the twin — it has no 'The ' to cost it similarity — so
    compare with the leading article stripped and, among equally close names,
    take the one with the deepest catalog: the real artist's, not the twin's.

    Matched artists are cached to disk so a re-scan skips the search call;
    flush_resolve_cache() persists new matches.
    """
    global _resolve_cache_dirty
    cache = _load_resolve_cache()
    hit = cache.get(query)
    if hit is not None:
        return hit[0], hit[1]
    try:
        results = search_artists(query, token, limit=cfg.ARTIST_LOOKUP_LIMIT)
    except AuthLost:
        raise
    except Exception as e:
        log.info(f"  artist search failed for '{query}': {e}")
        return None, None
    q = _strip_leading_article(query)

    def match_score(a):
        return similarity(_strip_leading_article(a.get("name", "")), q)

    qualifying = [a for a in results if match_score(a) >= cfg.ARTIST_NAME_THRESH]
    if not qualifying:
        return None, None
    best = max(qualifying,
               key=lambda a: (match_score(a), a.get("albums_count") or 0))
    aid, aname = best.get("id"), best.get("name")
    cache[query] = [aid, aname]
    _resolve_cache_dirty = True
    return aid, aname


def _missing_albums(artist_id, artist_name, token, partial_only=False,
                    hidden=None):
    """Yield Qobuz album dicts that need attention — either entirely
    absent from the library, or present on disk with track-level gaps.
    Partial albums get a `_partial_missing_count` key set on the dict
    so the candidate detail can show 'gap-fill: N missing'.

    With partial_only, fully-missing albums are skipped — only on-disk
    albums with track gaps are yielded (the album-fill use case).

    `hidden` is a preloaded hidden-store (library.hidden.load()) supplied by
    the bulk library walk so dismissed albums are skipped. The single-artist
    Artist scan passes None — typing a name overrides the memory.
    """
    catalog, total = get_artist_albums(artist_id, token,
                                       limit=cfg.ARTIST_CATALOG_LIMIT)
    if total and total > len(catalog):
        log.info(f"  Qobuz lists {total} albums; scanning the first "
                 f"{len(catalog)}.")
    pairs = dedup_album_versions(catalog, prefer_hires=cfg.PREFER_HIRES)
    pairs = filter_compilation_albums(pairs, artist_name)
    pairs = filter_short_releases(pairs, cfg.MISSING_ALBUMS_MIN_TRACKS)
    for album, _n_versions in pairs:
        if hidden is not None and hidden_mod.is_hidden(
                hidden_mod.SCOPE_MISSING, artist_name, album.get("title"), hidden):
            continue
        existing, _album_dir = find_existing_tracks(album)
        if not existing:
            if not partial_only:
                yield album
            continue
        # On-disk: check for track gaps. process_album re-derives the
        # gap when the user approves, so we don't need to thread the
        # missing set through the candidate — just count it for the UI.
        qobuz_tracks = (album.get("tracks") or {}).get("items") or []
        if not qobuz_tracks:
            # get_artist_albums returns tracks_count but not the track list,
            # so the gap check would be dead without materializing it. Only
            # paid for on-disk albums (fully-missing ones short-circuit above).
            try:
                full = get_album(album.get("id"), token)
            except AuthLost:
                raise
            except Exception:
                full = None
            qobuz_tracks = ((full or {}).get("tracks") or {}).get("items") or []
        if not qobuz_tracks:
            continue
        missing, present = compute_missing(qobuz_tracks, existing)
        if not present:
            # Zero track overlap: the folder fuzz-resolved to the wrong album.
            # Yielding it would offer to download a whole album over a folder
            # that only looks similarly named. A real collaboration filed
            # elsewhere overlaps on tracks, so this only drops false matches.
            continue
        if missing:
            album["_partial_missing_count"] = len(missing)
            yield album


def _add_album_candidate(job, album, artist_name, selected=True):
    year = album_year(album)
    partial_n = album.get("_partial_missing_count")
    if partial_n:
        detail = (f"{year or '?'} · {album_quality_label(album)} · "
                  f"gap-fill: {partial_n} missing of "
                  f"{album.get('tracks_count') or '?'}")
    else:
        tc = album.get('tracks_count') or '?'
        detail = (f"{year or '?'} · {album_quality_label(album)} · "
                  f"{tc} track{'s' if tc != 1 else ''}")
    job.add_candidate(
        kind="album",
        title=album.get("title") or "?",
        artist=artist_name,
        detail=detail,
        payload={"album_id": album.get("id"), "year": year},
        selected=selected,
    )



def _record_last_scan():
    try:
        cfg.LAST_SCAN_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _load_scan_seen(mode):
    """Fingerprints the last completed walk of this mode surfaced, or None if
    there's no prior run to compare against (first scan badges nothing)."""
    try:
        data = json.loads(cfg.SCAN_SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bucket = data.get(mode) if isinstance(data, dict) else None
    return set(bucket) if isinstance(bucket, list) else None


def _save_scan_seen(mode, fingerprints):
    try:
        data = json.loads(cfg.SCAN_SEEN_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[mode] = sorted(fingerprints)
    try:
        cfg.SCAN_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.SCAN_SEEN_FILE.with_suffix(cfg.SCAN_SEEN_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(cfg.SCAN_SEEN_FILE)
    except OSError:
        pass


def _flag_new_since_last_scan(job, mode):
    """Badge candidates whose album wasn't surfaced by the previous walk, then
    record this walk's set for next time. First-ever run badges nothing (no
    baseline to diff). Skipped on a cancelled scan so a partial run can't
    poison the baseline."""
    seen_now = set()
    fps = {}
    for c in job.candidates:
        fp = hidden_mod.album_fingerprint(c.get("artist"), c.get("title"))
        if fp:
            seen_now.add(fp)
            fps[c["cid"]] = fp
    prev = _load_scan_seen(mode)
    if prev is not None:
        for c in job.candidates:
            fp = fps.get(c["cid"])
            if fp and fp not in prev:
                c["payload"]["is_new"] = True
    _save_scan_seen(mode, seen_now)


def dismiss_albums(job, artist, keep_cids, scope=hidden_mod.SCOPE_MISSING):
    """Hide every album for `artist` that isn't in `keep_cids`, in `scope`.

    The hidden albums are recorded in the durable store so future bulk walks of
    that scope skip them, then dropped from this job's review list. Independent
    of downloading — the kept candidates stay for the normal approve flow.
    Returns the number hidden.
    """
    from qobuz_librarian.web import job_persistence

    keep = set(keep_cids)
    # Snapshot + mutate under the lock in one go: a live scan appends candidates
    # from the worker thread, so reading job.candidates and replacing it in
    # separate steps could drop a concurrently-added album.
    with job._lock:
        to_hide = [c for c in job.candidates
                   if c.get("artist") == artist and c.get("cid") not in keep]
        if not to_hide:
            return 0
        drop = {c["cid"] for c in to_hide}
        survivors = [c for c in job.candidates if c["cid"] not in drop]
        # The hide POST carries the currently-ticked cids; mirror them onto the
        # survivors so "hide the rest" keeps the album the user meant to download.
        for c in survivors:
            c["selected"] = c["cid"] in keep
        job.candidates = survivors
        specs = [(c.get("artist"), c.get("title"),
                  (c.get("payload") or {}).get("year")) for c in to_hide]
    # File write + persist outside the lock — neither needs it, and hide does
    # disk I/O that shouldn't stall the scan thread's next add_candidate.
    hidden_mod.hide(scope, specs)
    job_persistence.persist(job)
    return len(to_hide)


# ── Scans ─────────────────────────────────────────────────────────────────────

def scan_artist(job, query, token):
    clear_scan_caches()
    log.info(f"Resolving artist '{query}'…")
    artist_id, artist_name = resolve_artist(query, token)
    if not artist_id:
        log.info(f"  No confident Qobuz match for '{query}'.")
        job.summary = (f"No Qobuz match for “{query}”. Check the spelling, or "
                       "try the artist's exact name as Qobuz lists it.")
        return
    log.info(f"  Matched: {artist_name}. Scanning catalog for gaps…")
    n = 0
    for album in _missing_albums(artist_id, artist_name, token):
        _add_album_candidate(job, album, artist_name)
        n += 1
    log.info(f"  {plural(n, 'missing album')} found for {artist_name}.")
    flush_resolve_cache()
    _record_last_scan()


def _scan_library_artist(artist_dir, token, partial_only, hidden):
    """Worker: resolve one artist and collect its missing albums. Runs in a
    pool thread (its own HTTP session); returns plain data so the caller adds
    candidates serially — keeping job.candidates single-writer."""
    name = artist_dir.name
    artist_id, artist_name = resolve_artist(name, token)
    if not artist_id:
        return name, None, []
    albums = list(_missing_albums(artist_id, artist_name, token,
                                  partial_only=partial_only, hidden=hidden))
    return name, artist_name, albums


def scan_library(job, token, partial_only=False):
    clear_scan_caches()
    artists = list_library_artists()
    if not artists:
        log.info("No artist folders found under MUSIC_ROOT.")
        log.info("  Expected layout: $MUSIC_ROOT/<Artist>/<Album (Year)>/<track>.flac")
        log.info("  Check that QL_MUSIC_DIR in your .env points at the right place.")
        return
    target = "track gaps in owned albums" if partial_only else "missing albums"
    log.info(f"Scanning {plural(len(artists), 'library artist')} for {target}")
    # Snapshot the dismissed-album memory once so the parallel workers filter
    # against a single consistent view (a dismiss landing mid-scan is picked up
    # on the next run, not raced into this one).
    hidden = hidden_mod.load()
    total = 0
    done = 0
    n = len(artists)
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # Resolve/scan artists in parallel (each worker has its own HTTP session),
    # but collect results and write candidates on this one thread so the
    # candidate list and progress stay single-writer.
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="libscan") as ex:
        futures = {ex.submit(_scan_library_artist, ad, token, partial_only,
                             hidden): ad
                   for ad in artists}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                log.info("Cancelled — stopping scan.")
                break
            done += 1
            try:
                name, artist_name, albums = fut.result()
            except AuthLost:
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                log.info(f"    skipped {futures[fut].name}: {e}")
                job.push_progress("Scanning library", done, n, futures[fut].name,
                                  found=total)
                continue
            for album in albums:
                # Library is a discovery list — leave candidates unticked so a
                # single click can't queue hundreds nobody reviewed.
                _add_album_candidate(job, album, artist_name, selected=False)
                total += 1
            # Add the albums before the progress tick so a hit lands the live
            # preview the same moment the running total moves.
            hit = ({"artist": artist_name or name, "albums": len(albums)}
                   if albums else None)
            job.push_progress("Scanning library", done, n, artist_name or name,
                              found=total, hit=hit)
            if albums:
                tail = "with gaps" if partial_only else "to fill"
                log.info(f"  {artist_name} — {plural(len(albums), 'album')} {tail}")
    flush_resolve_cache()
    _record_last_scan()
    if not job.cancel_requested:
        _flag_new_since_last_scan(job, "partial" if partial_only else "missing")
    if partial_only:
        log.info(f"Done. {plural(total, 'album')} with track gaps across the library.")
    else:
        log.info(f"Done. {plural(total, 'missing album')} across the library.")


# ── Execute ───────────────────────────────────────────────────────────────────

def execute_albums(job, chosen, token):
    """Download each selected album via the normal process_album path."""
    from qobuz_librarian.modes.process import process_album
    from qobuz_librarian.web.jobs import staging_lock

    # The web worker runs jobs back-to-back; a directory listing cached by
    # a previous job would otherwise be reused even though folders may
    # have moved since.
    clear_scan_caches()
    args = build_args()
    _benign = {"already_complete", "skipped_already_higher_quality", "dry_run",
               "user_skipped", "lossy_only", "no_tracks"}
    ok = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            remaining = len(chosen) - processed
            log.info(f"Cancelled — {ok} downloaded, {remaining} not started.")
            return
        processed = i
        album_id = cand["payload"].get("album_id")
        label = f"[{i}/{len(chosen)}] {cand.get('artist','')} — {cand['title']}"
        log.info(label)
        try:
            full = get_album(album_id, token)
        except Exception as e:
            log.info(f"  could not fetch album {album_id}: {e}")
            failed += 1
            continue
        try:
            with staging_lock():
                result = process_album(full, args, allow_force=False,
                                       already_confirmed=True, token=token)
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        if result and result.get("imported") and result.get("n_ok", 0) > 0:
            ok += 1
        elif not (result and result.get("result") in _benign):
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    log.info(f"Finished — {ok}/{plural(len(chosen), 'album')} downloaded and imported.")
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} didn't finish — see the log."


# ── Upgrade flow ──────────────────────────────────────────────────────────────

def _scan_artist_upgrades(artist_dir, token, args, capped):
    """Worker: collect upgrade candidates for one artist. Runs in a pool
    thread (its own HTTP session); returns plain data so the caller adds
    candidates serially — keeping job.candidates single-writer."""
    from qobuz_librarian.quality.decision import scan_artist_for_upgrades
    name = artist_dir.name
    cands = scan_artist_for_upgrades(name, artist_dir, token, args,
                                     capped=capped)
    return name, cands


def scan_upgrades(job, token):
    """Scan the library for albums Qobuz can serve at higher quality."""
    from qobuz_librarian.quality.decision import load_capped

    clear_scan_caches()
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        log.info("No artist folders found under MUSIC_ROOT.")
        log.info("  Check that QL_MUSIC_DIR in your .env points at the right place.")
        return
    args = build_args()
    capped = load_capped()
    # Upgrades the user dismissed ("I'm happy with my copy") — independent of
    # the auto-`capped` memory and of the missing-album hides.
    hidden = hidden_mod.load()
    log.info(f"Scanning {plural(len(artists), 'artist')} for quality upgrades")
    total = 0
    done = 0
    n = len(artists)
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # Same parallel shape as scan_library: each artist needs 2–3 Qobuz calls,
    # so a serial loop makes the user wait through hundreds of round-trips
    # before the first result. Workers fan out; candidates are added on this
    # thread so the list stays single-writer.
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="upgradescan") as ex:
        futures = {ex.submit(_scan_artist_upgrades, ad, token, args, capped): ad
                   for ad in artists}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                log.info("Cancelled — stopping scan.")
                break
            done += 1
            name = futures[fut].name
            try:
                name, cands = fut.result()
            except AuthLost:
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                log.info(f"    skipped {name}: {e}")
                job.push_progress("Scanning for upgrades", done, n, name, found=total)
                continue
            added = 0
            for c in cands:
                album = c["qobuz_album"]
                title = album.get("title") or "?"
                if hidden_mod.is_hidden(hidden_mod.SCOPE_UPGRADE, name, title, hidden):
                    continue
                np_, nt = c.get("n_present", 0), c.get("n_total", 0)
                part = f" · {np_}/{nt} tracks" if nt and np_ < nt else ""
                # Unticked by default — like the gap scan, one click shouldn't
                # re-rip hundreds of albums nobody reviewed.
                job.add_candidate(
                    kind="upgrade",
                    title=title,
                    artist=name,
                    detail=f"{c.get('existing_quality_label','?')} → "
                           f"{c.get('target_quality_label','?')}{part}",
                    payload={"candidate": c, "year": album_year(album)},
                    selected=False,
                )
                total += 1
                added += 1
            hit = {"artist": name, "albums": added} if added else None
            job.push_progress("Scanning for upgrades", done, n, name,
                              found=total, hit=hit)
            if added:
                log.info(f"  {name} — {plural(added, 'album')} to upgrade")
    if not job.cancel_requested:
        _flag_new_since_last_scan(job, "upgrade")
    log.info(f"Done. {plural(total, 'upgradeable album')} found.")


def execute_upgrades(job, chosen, token):
    """Re-rip the present tracks of each chosen album at higher quality."""
    from qobuz_librarian.modes.process import process_album
    from qobuz_librarian.web.jobs import staging_lock

    clear_scan_caches()
    args = build_args()
    # Explicit upgrade: enable the replace path for this run only, and turn
    # off per-album consolidation prompts (the CLI upgrade walk does the same).
    args.auto_upgrade = True
    args.consolidate = False
    # Outcomes that aren't a failure — the album just didn't need (or couldn't
    # safely take) an upgrade. A backup-failed abort is NOT here: that's a real
    # failure the user should see.
    _skip = {"upgrade_only_no_op", "skipped_already_higher_quality",
             "skipped_has_extras", "lossy_only", "no_tracks",
             "user_skipped", "dry_run", "cancelled"}
    ok = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            remaining = len(chosen) - processed
            log.info(f"Cancelled — {ok} upgraded, {remaining} not started.")
            break
        processed = i
        c = cand["payload"]["candidate"]
        album = c["qobuz_album"]
        log.info(f"[{i}/{len(chosen)}] {cand.get('artist','')} — "
                 f"{album.get('title') or '?'}")
        try:
            with staging_lock():
                result = process_album(album, args, allow_force=False,
                                       already_confirmed=True,
                                       upgrade_only=True, token=token)
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        _res = (result or {}).get("result")
        if result and result.get("imported") and _res not in (
                _skip | {"upgrade_aborted_backup_failed"}):
            ok += 1
        elif _res not in _skip:
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    log.info(f"Finished — upgraded {ok}/{plural(len(chosen), 'album')}.")
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} couldn't be upgraded — see the log."


# ── Repair flow ───────────────────────────────────────────────────────────────

def scan_repairs(job, token):
    """Scan every album for ISRC-verified truncated FLACs."""
    from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs

    clear_scan_caches()
    artists = list_library_artists()
    if not artists:
        log.info("No artist folders found under MUSIC_ROOT.")
        log.info("  Check that QL_MUSIC_DIR in your .env points at the right place.")
        return
    log.info(f"Scanning {plural(len(artists), 'artist')} for truncated files")
    total = 0
    for i, artist_dir in enumerate(artists, 1):
        if job.cancel_requested:
            log.info("Cancelled — stopping scan.")
            return
        name = artist_dir.name
        album_dirs = list_artist_album_dirs(artist_dir)
        job.push_progress("Checking for damaged files", i, len(artists), name)
        for album_dir in album_dirs:
            if job.cancel_requested:
                log.info("Cancelled — stopping scan.")
                return
            try:
                scan = scan_dir_for_isrc_repairs(album_dir, token, deep=False)
            except AuthLost:
                raise
            except Exception as e:
                log.info(f"    skipped {album_dir.name}: {e}")
                continue
            truncated = scan["verified_truncated"]
            if truncated:
                job.add_candidate(
                    kind="repair",
                    title=album_dir.name,
                    artist=name,
                    detail=f"{plural(len(truncated), 'truncated track')}",
                    payload={"album_dir": str(album_dir),
                             "artist_name": name,
                             "verified_truncated": truncated},
                )
                total += 1
            # Damaged files with no readable ISRC can't be surgically refilled
            # (no anchored Qobuz lookup). Identify the album from the folder
            # and offer a whole-album re-download instead — the user sees the
            # matched album in the review and confirms before it runs.
            suspicious = [e for e in scan.get("no_isrc_tag", [])
                          if e.get("diagnostic")]
            if suspicious:
                matched = find_qobuz_album_for_dir(album_dir, name, token)
                if matched and matched.get("id"):
                    m_title = matched.get("title") or album_dir.name
                    m_year = album_year(matched) or "?"
                    job.add_candidate(
                        kind="redownload",
                        title=album_dir.name,
                        artist=name,
                        detail=(f"{plural(len(suspicious), 'damaged file')} "
                                f"can't be verified by ID — re-download the whole "
                                f"album fresh as “{m_title}” ({m_year})"),
                        payload={"album_dir": str(album_dir),
                                 "artist_name": name,
                                 "album_id": matched.get("id"),
                                 "matched_title": m_title},
                    )
                    total += 1
                else:
                    for e in suspicious:
                        log.info(f"    ⚠ {album_dir.name} — "
                                 f"{e.get('title') or '?'}: {e['diagnostic']}; "
                                 "couldn't match this folder to a Qobuz album "
                                 "to re-download — check by hand.")
        time.sleep(cfg.ARTIST_API_DELAY)
    log.info(f"Done. {plural(total, 'album')} flagged.")


def _redownload_damaged_album(payload, token):
    """Re-fetch a whole album whose damaged file couldn't be ID-verified.

    The folder is moved aside first so beets imports a clean copy instead of
    colliding with the broken files (the --force path can't be used here: it
    needs an interactive deletion confirm the web has no way to answer). If
    the re-download doesn't complete, the original folder is moved back so the
    user is never left worse off.
    """
    import shutil as _shutil

    from qobuz_librarian.library.backup import (
        backup_album_dir,
        restore_upgrade_backup,
    )
    from qobuz_librarian.modes.process import process_album
    from qobuz_librarian.web.jobs import staging_lock

    log.info("  The damaged file can't be verified by its ID, so the whole "
             "album is being re-downloaded fresh from Qobuz.")
    full = get_album(payload["album_id"], token)
    album_dir = Path(payload["album_dir"])
    backup = backup_album_dir(album_dir) if album_dir.exists() else None
    if album_dir.exists() and backup is None:
        log.info("  Couldn't safely move the existing folder out of the way, "
                 "so leaving this album alone to avoid damaging it. "
                 "See the log above.")
        return {"imported": False, "n_ok": 0, "result": "backup_failed"}
    try:
        with staging_lock():
            result = process_album(full, build_args(), allow_force=False,
                                   already_confirmed=True, token=token) or {}
    except Exception:
        if backup:
            restore_upgrade_backup(backup, album_dir)
        raise
    succeeded = bool(result.get("imported")) and result.get("n_ok", 0) > 0
    if backup:
        if succeeded:
            _shutil.rmtree(backup, ignore_errors=True)
        else:
            log.info("  Re-download didn't complete — restoring the original "
                     "album folder.")
            restore_upgrade_backup(backup, album_dir)
    return result


def execute_repairs(job, chosen, token):
    """Refill ISRC-verified truncated tracks, or re-download whole albums
    whose damage couldn't be ID-verified — depending on each candidate."""
    from pathlib import Path

    from qobuz_librarian.modes.repair import repair_album_dir
    from qobuz_librarian.web.jobs import staging_lock

    clear_scan_caches()
    args = build_args()
    fixed = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            remaining = len(chosen) - processed
            log.info(f"Cancelled — {fixed} repaired, {remaining} not started.")
            return
        processed = i
        p = cand["payload"]
        log.info(f"[{i}/{len(chosen)}] {p['artist_name']} — {cand['title']}")
        try:
            if cand.get("kind") == "redownload":
                # _redownload_damaged_album takes the staging lock itself.
                result = _redownload_damaged_album(p, token)
            else:
                with staging_lock():
                    result = repair_album_dir(Path(p["album_dir"]),
                                              p["verified_truncated"],
                                              p["artist_name"], args, token)
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        # Each chosen album was flagged as damaged, so anything that didn't end
        # up downloaded-and-imported is a real failure.
        if result and result.get("n_ok", 0) > 0 and result.get("imported"):
            fixed += 1
        else:
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    log.info(f"Finished — repaired {fixed}/{plural(len(chosen), 'album')}.")
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} couldn't be repaired — see the log."


def run_lyric_retry(job, token):
    """Retry lyric fetching for tracks queued from a previous failed run."""
    from qobuz_librarian.integrations.lyrics import (
        HAVE_LYRIC_FETCH,
        _refresh_lyric_retry,
        load_lyric_retry,
        lyric_fetch,
        save_lyric_retry,
    )

    paths = load_lyric_retry()
    if not paths:
        log.info("No tracks queued for lyric retry.")
        return

    if not HAVE_LYRIC_FETCH:
        log.info("lyric_fetch is unavailable — manifest preserved.")
        return

    existing = [Path(p) for p in paths if Path(p).exists()]
    dropped = len(paths) - len(existing)
    if dropped:
        log.info(f"{dropped} queued path(s) no longer on disk — skipping.")
    if not existing:
        save_lyric_retry([])
        log.info("All queued files are missing; manifest cleared.")
        return

    log.info(f"Retrying lyrics on {plural(len(existing), 'track')} ...")
    try:
        lyric_fetch.fetch_for_paths(
            existing, log=log,
            providers=cfg.LYRICS_PROVIDERS or None,
            lyrics_format=cfg.LYRICS_FORMAT,
            state_path=cfg.LYRIC_FETCH_STATE_FILE,
        )
    except Exception as e:
        log.info(f"Retry failed: {e} — manifest preserved.")
        return

    _refresh_lyric_retry(existing)
    remaining = load_lyric_retry()
    if remaining:
        log.info(f"{plural(len(remaining), 'track')} still unresolved — will retry next time.")
    else:
        log.info("All retried tracks resolved.")


# ── Library migration ──────────────────────────────────────────────────────────

def scan_migration(job, src, dest, *, use_acoustid):
    """Analyze the source library and attach one candidate per placeable album.

    Placeable albums become the review list (grouped by artist); files that
    can't be identified or that would collide are reported in the summary and
    left untouched. A preview manifest is written to the destination so the plan
    is auditable before anything is copied.
    """
    from qobuz_librarian.library import migrate as engine

    src, dest = Path(src), Path(dest)
    items = engine.collect_items(
        src, use_acoustid=use_acoustid,
        cancel_check=lambda: job.cancel_requested,
        progress=job.push_progress)
    if job.cancel_requested:
        return
    plan = engine.build_plan(items, dest)

    manifest = dest / "migration-manifest.csv"
    try:
        engine.write_manifest(plan, manifest)
    except OSError as exc:
        log.info(f"Couldn't write the preview manifest: {exc}")

    groups: dict = {}
    for entry in plan.placed:
        # dest_rel is <artist>/<album (year)>/[Disc N/]<track>; group by album dir.
        key = (entry.dest_rel.parts[0], entry.dest_rel.parts[1])
        groups.setdefault(key, []).append(entry)
    for (artist, album), entries in sorted(groups.items()):
        job.add_candidate(
            kind="migrate",
            title=album,
            artist=artist,
            detail=f"{plural(len(entries), 'track')} → {artist}/{album}",
            payload={"entries": [(str(e.source), str(e.dest_rel)) for e in entries]},
        )

    s = plan.summary()
    parts = [f"{plural(s['place'], 'file')} ready to copy"]
    if s["unplaceable"]:
        parts.append(f"{s['unplaceable']} couldn't be identified")
    if s["collision"]:
        parts.append(f"{s['collision']} skipped to avoid name collisions")
    job.summary = ("; ".join(parts) + ". Unidentified and skipped files stay "
                   f"where they are. Full plan written to {manifest}.")
    log.info(job.summary)


def execute_migration(job, chosen, dest, *, in_place):
    """Copy (or move) the files behind the approved albums into the layout."""
    from qobuz_librarian.library import migrate as engine

    dest = Path(dest)
    entries = []
    for c in chosen:
        for src_s, dest_s in c.get("payload", {}).get("entries", []):
            entries.append(engine.PlanEntry(
                source=Path(src_s), status=engine.PLACE, dest_rel=Path(dest_s)))
    if not entries:
        job.push_line("Nothing selected — nothing to copy.")
        return

    plan = engine.MigrationPlan(dest_root=dest, entries=entries)
    result = engine.execute_plan(
        plan, in_place=in_place,
        cancel_check=lambda: job.cancel_requested,
        progress=job.push_progress)
    try:
        engine.write_manifest(plan, dest / "migration-manifest.csv")
    except OSError:
        pass

    verb = "moved" if in_place else "copied"
    parts = [f"{plural(result.copied, 'file')} {verb} into {dest}"]
    if result.skipped:
        parts.append(f"{result.skipped} skipped (already present)")
    if result.failed:
        parts.append(f"{result.failed} failed — see the log")
    if result.cancelled:
        parts.append("stopped early")
    job.summary = "; ".join(parts) + "."
    log.info(job.summary)
