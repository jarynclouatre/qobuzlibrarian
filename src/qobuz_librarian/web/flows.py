"""Scan / execute logic behind the web Artist and Library flows.

These wrap the same engine the CLI uses (catalog matching, gap detection,
process_album) but without any terminal prompts — a scan attaches review
candidates to the job, and execution runs over the candidates the user kept.
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzUnavailable
from qobuz_librarian.api.search import get_album
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library import new_releases as new_releases_mod
from qobuz_librarian.library import repair_cache, scan_checkpoint
from qobuz_librarian.library.catalog import (
    album_quality_label,
    album_year,
    find_album_dir_filesystem,
    find_qobuz_album_for_dir,
    is_lossless_album,
)
from qobuz_librarian.library.discovery import (
    DiscoveryOpts,
    find_missing_for_artist,
    find_new_releases_for_artist,
    flush_resolve_cache,
)
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_librarian.library.tags import VA_NORMALIZED, normalize
from qobuz_librarian.ui_cli.colors import format_size
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


def _add_album_candidate(job, album, artist_name, selected=True, is_new=False):
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
    payload = {"album_id": album.get("id"), "year": year}
    if is_new:
        payload["is_new"] = True
    job.add_candidate(
        kind="album",
        title=album.get("title") or "?",
        artist=artist_name,
        detail=detail,
        payload=payload,
        selected=selected,
    )


def _readd_candidate(job, c):
    """Re-add a candidate restored from a scan checkpoint, with a fresh cid."""
    job.add_candidate(kind=c.get("kind", "album"), title=c.get("title", "?"),
                      artist=c.get("artist", ""), detail=c.get("detail", ""),
                      payload=c.get("payload") or {}, selected=bool(c.get("selected")))


def _cap_note(job) -> str:
    """A truncation notice appended to a scan summary when the candidate list hit
    the in-memory cap, so a summary never implies more results are reviewable
    than were actually kept. Empty when nothing was dropped."""
    if not job.candidate_cap_hit:
        return ""
    return (f" Showing the first {len(job.candidates):,} — the scan hit the "
            f"{job.CANDIDATE_CAP:,} result cap. Scan a single artist, or raise "
            "JOB_CANDIDATE_CAP, to see the rest.")


def _add_gap_candidate(job, gap, artist_name, selected=False, is_new=False):
    """Turn an engine AlbumGap into a review candidate. A partial gap carries
    its missing-track count so the detail reads 'gap-fill: N missing'."""
    album = gap.qobuz_album
    if gap.on_disk_dir is not None:
        album = {**album, "_partial_missing_count": gap.missing_count}
    _add_album_candidate(job, album, artist_name, selected=selected, is_new=is_new)


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
    # Snapshot under the lock — the scan worker appends candidates to this
    # list and we walk it twice here, which without a snapshot is not safe
    # against a same-instant append. `dismiss_albums` uses the same pattern.
    with job._lock:
        candidates = list(job.candidates)
    seen_now = set()
    fps = {}
    for c in candidates:
        fp = hidden_mod.album_fingerprint(c.get("artist"), c.get("title"))
        if fp:
            seen_now.add(fp)
            fps[c["cid"]] = fp
    prev = _load_scan_seen(mode)
    if prev is not None:
        for c in candidates:
            fp = fps.get(c["cid"])
            if fp and fp not in prev:
                c["payload"]["is_new"] = True
    _save_scan_seen(mode, seen_now)


def dismiss_albums(job, artist, scope=hidden_mod.SCOPE_MISSING):
    """Hide ``artist``'s albums that aren't currently selected, in ``scope``.

    Selection is server-backed (saved as the user ticks), so "hide the rest"
    means: of this artist's candidates, hide the ones whose saved `selected`
    flag is off and keep the ticked ones. Other artists' candidates and their
    selections are never touched — critical now that pagination means most of
    them aren't even on the page that triggered the hide.

    The hidden albums are recorded in the durable store so future bulk walks of
    that scope skip them, then dropped from this job's review list. Returns the
    number hidden.
    """
    from qobuz_librarian.web import job_persistence

    # Snapshot + mutate under the lock in one go: a live scan appends candidates
    # from the worker thread, so reading job.candidates and replacing it in
    # separate steps could drop a concurrently-added album.
    with job._lock:
        to_hide = [c for c in job.candidates
                   if c.get("artist") == artist and not c.get("selected")]
        if not to_hide:
            return 0
        drop = {c["cid"] for c in to_hide}
        # Only this artist's unselected candidates leave; every other
        # candidate (and its saved selection) is preserved untouched.
        job.candidates = [c for c in job.candidates if c["cid"] not in drop]
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
    # Fresh: an explicit single-artist scan should see just-released albums, not
    # a week-old cached catalog. No hidden filter — asking for an artist by name
    # is a deliberate request to see everything, dismissed albums included.
    seen = new_releases_mod.load().get("seen") or {}
    result = find_missing_for_artist(
        query, token=token, opts=DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES),
        single_store=hidden_mod.load(), fresh=True)
    if not result.artist_id:
        log.info(f"  No confident Qobuz match for '{query}'.")
        job.summary = (f"No Qobuz match for “{query}”. Check the spelling, or "
                       "try the artist's exact name as Qobuz lists it.")
        return
    log.info(f"  Matched: {result.artist_name}. Scanning catalog for gaps…")
    # Flag (and pre-tick) albums new since the last time this artist was checked,
    # sharing the baseline with the library-wide new-release check. The rest stay
    # unticked — one artist can have dozens of albums, so don't queue the lot.
    aid = str(result.artist_id)
    baseline = seen.get(aid)
    known = set(baseline or [])
    n_new = 0
    for gap in result.gaps:
        is_new = baseline is not None and str(gap.qobuz_album.get("id")) not in known
        if is_new:
            n_new += 1
        _add_gap_candidate(job, gap, result.artist_name,
                           selected=is_new, is_new=is_new)
    # Don't overwrite this artist's baseline from a transient short-page fetch —
    # a partial discography would dump the dropped albums as "new" next check.
    if not result.catalog_incomplete:
        current_ids = [str(a["id"]) for a in result.catalog
                       if is_lossless_album(a) and a.get("id") is not None]
        new_releases_mod.record_artist_seen(aid, current_ids)
    msg = f"  {plural(len(result.gaps), 'missing album')} found for {result.artist_name}"
    if n_new:
        msg += f" — {n_new} new since your last check, pre-ticked"
    log.info(msg + ".")
    flush_resolve_cache()
    _record_last_scan()


def _scan_library_artist(artist_dir, token, partial_only, hidden):
    """Worker: find one artist's gaps. Runs in a pool thread (its own HTTP
    session); returns plain data so the caller adds candidates serially —
    keeping job.candidates single-writer. Also returns the artist's id and its
    lossless catalog ids so the caller can seed the new-release baseline (the
    discography is already fetched here)."""
    result = find_missing_for_artist(
        artist_dir.name, token=token,
        opts=DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES),
        artist_dir=artist_dir, hidden=hidden, single_store=hidden,
        want_missing=not partial_only)
    artist_id = str(result.artist_id) if result.artist_id else None
    # None signals "don't seed a baseline" — a transient short-page fetch isn't
    # the whole discography, so seeding it would later dump the dropped albums
    # as "new". The gaps are still surfaced this scan; the artist just stays
    # un-baselined until a complete fetch.
    catalog_ids = None if result.catalog_incomplete else [
        str(a["id"]) for a in result.catalog
        if is_lossless_album(a) and a.get("id") is not None]
    return artist_dir.name, result.artist_name, result.gaps, artist_id, catalog_ids


_CHECKPOINT_EVERY = 15  # artists between progress saves (resume granularity)
# Seconds between "still scanning" proof-of-life log lines during the whole-
# library repair sweep (see scan_repairs). A clean library logs nothing for
# minutes (only problems print), which reads as a hang — this keeps the web
# console alive. Long enough that a fast cached re-scan doesn't flood the log.
_REPAIR_HEARTBEAT_SECS = 12


def scan_library(job, token, partial_only=False):
    clear_scan_caches()
    # Drop the Various-Artists folder: it has no single Qobuz artist catalog to
    # diff against, so a gap scan can only mis-resolve it. The upgrade/downsample
    # scans already filter it — this keeps the missing/partial scan consistent.
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        job.summary = ("No artist folders found under MUSIC_ROOT — check that "
                       "QL_MUSIC_DIR points at your library.")
        log.info("No artist folders found under MUSIC_ROOT.")
        log.info("  Expected layout: $MUSIC_ROOT/<Artist>/<Album (Year)>/<track>.flac")
        log.info("  Check that QL_MUSIC_DIR in your .env points at the right place.")
        return
    kind = "partial" if partial_only else "missing"
    # Resume an interrupted scan of this kind: skip the artists already done and
    # restore the albums they turned up, so we continue rather than restart.
    cp = scan_checkpoint.load(kind)
    resuming = cp is not None
    scanned = set(cp["scanned"]) if resuming else set()
    baseline_seen = dict(cp["seen"]) if resuming else {}
    total = 0
    # Snapshot the dismissed-album memory before restoring the checkpoint so
    # albums the user dismissed since the interruption are not re-added, and
    # so the parallel workers below see the same consistent view.
    hidden = hidden_mod.load()
    if resuming:
        for c in cp["candidates"]:
            if hidden_mod.is_hidden(hidden_mod.SCOPE_MISSING,
                                    c.get("artist"), c.get("title"), hidden):
                continue
            _readd_candidate(job, c)
            total += 1
        log.info(f"Resuming — {len(scanned)} artist(s) already scanned, "
                 f"{plural(total, 'album')} found so far.")
    target = "track gaps in owned albums" if partial_only else "missing albums"
    log.info(f"Scanning {plural(len(artists), 'library artist')} for {target}")
    todo = [ad for ad in artists if ad.name not in scanned]
    n = len(artists)
    done = len(scanned)
    since_save = 0
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # Resolve/scan artists in parallel (each worker has its own HTTP session),
    # but collect results and write candidates on this one thread so the
    # candidate list and progress stay single-writer.
    from qobuz_librarian.web.jobs import pool_initializer_kwargs
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="libscan",
                            **pool_initializer_kwargs()) as ex:
        futures = {ex.submit(_scan_library_artist, ad, token, partial_only,
                             hidden): ad
                   for ad in todo}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                log.info("Cancelled — stopping scan.")
                break
            done += 1
            try:
                name, artist_name, gaps, artist_id, catalog_ids = fut.result()
            except (AuthLost, QobuzUnavailable):
                # A lost token or an unreachable API isn't a per-artist hiccup —
                # cancel the rest and fail the scan rather than silently report a
                # partial library as the full picture. The checkpoint stays, so
                # the scan resumes once the token/network is back.
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                # A per-artist failure (not auth/outage) is left unscanned so a
                # resume retries it rather than baking in a transient miss.
                log.info(f"    skipped {futures[fut].name}: {e}")
                job.push_progress("Scanning library", done, n, futures[fut].name,
                                  found=total)
                continue
            scanned.add(name)
            if artist_id and catalog_ids is not None:
                baseline_seen[artist_id] = catalog_ids
            for gap in gaps:
                # Library is a discovery list — leave candidates unticked so a
                # single click can't queue hundreds nobody reviewed.
                _add_gap_candidate(job, gap, artist_name or name, selected=False)
                total += 1
            # Add the albums before the progress tick so a hit lands the live
            # preview the same moment the running total moves.
            hit = ({"artist": artist_name or name, "albums": len(gaps)}
                   if gaps else None)
            job.push_progress("Scanning library", done, n, artist_name or name,
                              found=total, hit=hit)
            if gaps:
                tail = "with gaps" if partial_only else "to fill"
                log.info(f"  {artist_name} — {plural(len(gaps), 'album')} {tail}")
            since_save += 1
            if since_save >= _CHECKPOINT_EVERY:
                since_save = 0
                scan_checkpoint.save(kind, scanned, job.candidates, baseline_seen)
    # Reached here only without an AuthLost/outage abort (that re-raises out
    # above, leaving the checkpoint for resume and not seeding the baseline).
    flush_resolve_cache()
    if job.cancel_requested:
        # Deliberate stop — discard this kind's progress so it isn't auto-resumed.
        scan_checkpoint.clear(kind)
    else:
        # Only a clean, complete crawl stamps "last scanned" — a cancelled scan
        # mustn't make the dashboard read as freshly scanned and suppress the
        # next automatic new-release check.
        _record_last_scan()
        _flag_new_since_last_scan(job, kind)
        # The crawl reached every artist cleanly — establish the new-release
        # baseline from the catalog snapshot (only the first time; the daily
        # check keeps it fresh after), and clear this kind's checkpoint.
        if not new_releases_mod.is_baseline_complete():
            new_releases_mod.seed_baseline(baseline_seen)
        scan_checkpoint.clear(kind)
    if job.cancel_requested:
        job.summary = (f"Stopped early — {plural(total, 'album')} found so far."
                       if total else "Stopped before anything turned up.")
    elif partial_only:
        job.summary = (f"{plural(total, 'album')} with track gaps across the library."
                       + _cap_note(job)
                       if total else "No track gaps found in your owned albums.")
    else:
        job.summary = (f"{plural(total, 'missing album')} across the library."
                       + _cap_note(job)
                       if total else
                       "No missing albums — your library matches each artist's "
                       "Qobuz catalog.")
    log.info(job.summary)


def scan_new_releases(job, token):
    """Surface albums that appeared in library artists' Qobuz catalogs since the
    last check and that the user doesn't own or hasn't hidden — pre-ticked, ready
    to download. Cheap (one catalog call per artist, no track fetches), so it's
    the quick "what's new" pass rather than the full gap scan."""
    clear_scan_caches()
    # Same VA exclusion as scan_library: the Various-Artists folder has no single
    # Qobuz catalog, so it can't yield meaningful "new releases".
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        job.summary = ("No artist folders found under MUSIC_ROOT — check that "
                       "QL_MUSIC_DIR points at your library.")
        log.info("No artist folders found under MUSIC_ROOT.")
        log.info("  Expected layout: $MUSIC_ROOT/<Artist>/<Album (Year)>/<track>.flac")
        log.info("  Check that QL_MUSIC_DIR in your .env points at the right place.")
        return
    state = new_releases_mod.load()
    seen = state.get("seen") or {}
    hidden = hidden_mod.load()
    opts = DiscoveryOpts(prefer_hires=cfg.PREFER_HIRES)
    log.info(f"Checking {plural(len(artists), 'artist')} for new releases…")
    total = 0
    done = 0
    n = len(artists)
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # This run's reached artists; merged over the prior baseline at the end (so a
    # run where some/all artists errored can't wipe their baselines and re-surface
    # everything — only artists actually reached get their snapshot refreshed).
    current_seen = {}
    from qobuz_librarian.web.jobs import pool_initializer_kwargs
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="newrel",
                            **pool_initializer_kwargs()) as ex:
        futures = {ex.submit(find_new_releases_for_artist, ad.name, token=token,
                             opts=opts, seen_by_id=seen, hidden=hidden,
                             single_store=hidden, artist_dir=ad): ad
                   for ad in artists}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                log.info("Cancelled — stopping check.")
                break
            done += 1
            try:
                result = fut.result()
            except (AuthLost, QobuzUnavailable):
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                log.info(f"    skipped {futures[fut].name}: {e}")
                job.push_progress("Checking for new releases", done, n,
                                  futures[fut].name, found=total)
                continue
            if result.artist_id and not getattr(result, "fetch_failed", False):
                current_seen[result.artist_id] = result.current_ids
            for gap in result.new_gaps:
                _add_gap_candidate(job, gap, result.artist_name,
                                   selected=True, is_new=True)
                total += 1
            hit = ({"artist": result.artist_name, "albums": len(result.new_gaps)}
                   if result.new_gaps else None)
            job.push_progress("Checking for new releases", done, n,
                              result.artist_name or futures[fut].name,
                              found=total, hit=hit)
            if result.new_gaps:
                log.info(f"  {result.artist_name} — "
                         f"{plural(len(result.new_gaps), 'new release')}")
    flush_resolve_cache()
    if not job.cancel_requested:
        # Merge over the prior baseline, don't replace it: an artist that errored
        # this run keeps its old entry (a bad run can't wipe the baseline), while
        # reached artists get refreshed. A clean check crawled every artist, so it
        # establishes the baseline too (a manual check before any library scan).
        new_releases_mod.mark_run({**seen, **current_seen}, complete=True)
    if job.cancel_requested:
        # A cancelled crawl only reached a fraction of the artists, so it can't
        # claim "No new releases" or "First check recorded" definitively.
        job.summary = ("Stopped early — partial check, "
                       f"{plural(total, 'new release')} found so far.")
        log.info(job.summary)
        return
    if total:
        job.summary = f"{plural(total, 'new release')} found across the library."
        log.info(f"Done. {plural(total, 'new release')} across the library.")
    elif not seen:
        job.summary = ("First check — recorded what each artist has now. "
                       "From here, new releases will show up here.")
        log.info("Baseline recorded — future checks will flag new releases.")
    else:
        job.summary = "No new releases since your last check."
        log.info("No new releases since your last check.")


# ── Execute ───────────────────────────────────────────────────────────────────

def _warn_if_download_short(job, album, artist_name, token):
    """Advisory post-download integrity check. The downloader already discards
    tracks that won't decode, but a clean truncation (decodes fine, its FLAC
    header rewritten to the short length) can slip past that gate. Re-verify the
    just-downloaded album's track lengths against Qobuz — cheap, and the result
    is cached for the next repair scan — and warn if a track came up short so it
    can be repaired now. Best-effort: it never blocks or alters the download, and
    any verify hiccup is swallowed (a successful download must not fail on it)."""
    try:
        album_dir = find_album_dir_filesystem(album)
        if album_dir is None:
            return
        outcome = _cached_album_outcome(album_dir, artist_name or "", token)
    except Exception:
        return
    if any(s.get("kind") == "repair" for s in outcome.get("specs", [])):
        log.info(f"  ⚠  {album.get('title') or 'this album'} downloaded, but a "
                 "track came up shorter than its Qobuz length — run Repair to "
                 "refill it.")


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
               "user_skipped", "lossy_only", "no_tracks", "skipped_has_extras",
               "cancelled"}
    ok = 0
    partial = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        album_id = cand["payload"].get("album_id")
        label = f"[{i}/{len(chosen)}] {cand.get('artist','')} — {cand['title']}"
        log.info(label)
        try:
            full = get_album(album_id, token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  could not fetch album {album_id}: {e}")
            failed += 1
            continue
        try:
            with staging_lock():
                result = process_album(full, args, allow_force=False,
                                       already_confirmed=True, token=token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        if result and result.get("imported") and result.get("n_ok", 0) > 0:
            # A partial (some tracks landed, some failed) isn't a full download —
            # count it apart so the summary doesn't claim it finished.
            if result.get("n_fail", 0) > 0:
                partial += 1
            else:
                ok += 1
                _warn_if_download_short(job, full, cand.get("artist", ""), token)
        elif not (result and result.get("result") in _benign):
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    if job.cancel_requested:
        job.summary = (f"Stopped early — {ok} downloaded, "
                       f"{len(chosen) - processed} not started.")
        log.info(job.summary)
        return
    parts = [f"{ok}/{plural(len(chosen), 'album')} downloaded and imported"]
    if partial:
        parts.append(f"{plural(partial, 'album')} only partly (some tracks failed)")
    job.summary = "Finished — " + ", ".join(parts) + "."
    log.info(f"Finished — {ok}/{plural(len(chosen), 'album')} downloaded and imported.")
    if partial:
        log.info(f"  {plural(partial, 'album')} downloaded only partly "
                 f"(some tracks failed) — see the log.")
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
        job.summary = ("No artist folders found under MUSIC_ROOT — check that "
                       "QL_MUSIC_DIR points at your library.")
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
    from qobuz_librarian.web.jobs import pool_initializer_kwargs
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="upgradescan",
                            **pool_initializer_kwargs()) as ex:
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
            except (AuthLost, QobuzUnavailable):
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
                # Store just the matched album's id and re-fetch it at execute
                # time (get_album is disk-cached), rather than persisting the
                # whole album dict the cache already holds. An edition swap keeps
                # its own id, so the id alone reproduces the exact edition to rip.
                # Unticked by default — like the gap scan, one click shouldn't
                # re-rip hundreds of albums nobody reviewed.
                job.add_candidate(
                    kind="upgrade",
                    title=title,
                    artist=name,
                    detail=f"{c.get('existing_quality_label','?')} → "
                           f"{c.get('target_quality_label','?')}{part}",
                    payload={"album_id": album.get("id"), "year": album_year(album)},
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
    if job.cancel_requested:
        job.summary = (f"Stopped early — {plural(total, 'album')} found so far."
                       if total else "Stopped before anything turned up.")
    else:
        job.summary = (f"{plural(total, 'upgradeable album')} Qobuz can serve "
                       "at higher quality." + _cap_note(job) if total else
                       "No upgrades — every album is already at the best quality "
                       "Qobuz offers.")
    log.info(job.summary)


def scan_upgrades_for_artist(job, artist_name, token):
    """Same upgrade scan as ``scan_upgrades`` but scoped to one artist's folder.

    Mirrors the per-artist library scan in shape: deliberate single-artist
    request, so the Hidden-upgrades store is NOT consulted (asking by name is
    "show me everything for this artist"). No pool — one artist is the whole
    job, so the parallel fan-out the whole-library scan needs is overkill.
    """
    from qobuz_librarian.library.discovery import resolve_artist_dir
    from qobuz_librarian.quality.decision import load_capped

    clear_scan_caches()
    artist_dir = resolve_artist_dir(artist_name)
    if artist_dir is None:
        job.summary = (f"No library folder for “{artist_name}”. "
                       "Check the spelling, or scan the whole library.")
        log.info(job.summary)
        return
    name = artist_dir.name
    args = build_args()
    capped = load_capped()
    log.info(f"Scanning {name} for quality upgrades")
    # The per-artist scans are single-step (one artist, one fetch), so push a
    # start frame so the SSE consumer's progress header reads "Checking for
    # upgrades · 0/1" instead of staying on whatever the previous job left,
    # then push the matching end frame at the close — keeps the running-job
    # page in sync with what the user thinks is happening.
    job.push_progress("Checking for upgrades", 0, 1, name)
    try:
        _, cands = _scan_artist_upgrades(artist_dir, token, args, capped)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception as e:
        # Set a terminal status + error so submit_scan's wrapper doesn't read the
        # zero-candidate SCANNING job as a clean run and report "Nothing to do".
        from qobuz_librarian.web.jobs import JobStatus
        log.info(f"  scan failed for {name}: {e}")
        job.error = f"Scan failed: {e}"
        job.summary = "Scan failed — see the log."
        job.status = JobStatus.FAILED
        return
    added = 0
    for c in cands:
        album = c["qobuz_album"]
        title = album.get("title") or "?"
        np_, nt = c.get("n_present", 0), c.get("n_total", 0)
        part = f" · {np_}/{nt} tracks" if nt and np_ < nt else ""
        job.add_candidate(
            kind="upgrade",
            title=title,
            artist=name,
            detail=f"{c.get('existing_quality_label','?')} → "
                   f"{c.get('target_quality_label','?')}{part}",
            payload={"album_id": album.get("id"), "year": album_year(album)},
            selected=False,
        )
        added += 1
    job.push_progress("Checking for upgrades", 1, 1, name, found=added)
    log.info(f"  {name} — {plural(added, 'album')} to upgrade")
    if not added:
        job.summary = f"No upgrades found for {name}."


def execute_upgrades(job, chosen, token):
    """Re-rip the present tracks of each chosen album at higher quality."""
    from qobuz_librarian.modes.process import process_album
    from qobuz_librarian.modes.upgrade import BENIGN_UPGRADE_RESULTS
    from qobuz_librarian.web.jobs import staging_lock

    clear_scan_caches()
    args = build_args()
    # Explicit upgrade: enable the replace path for this run only, and turn
    # off per-album consolidation prompts (the CLI upgrade walk does the same).
    args.auto_upgrade = True
    args.consolidate = False
    # Outcomes that aren't a failure — the album just didn't need (or couldn't
    # safely take) an upgrade. Shared with the CLI upgrade walk; a backup-failed
    # abort is deliberately excluded, so it counts as the real failure it is.
    _skip = BENIGN_UPGRADE_RESULTS
    ok = 0
    kept = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        album_id = cand["payload"].get("album_id")
        log.info(f"[{i}/{len(chosen)}] {cand.get('artist','')} — "
                 f"{cand.get('title') or '?'}")
        try:
            album = get_album(album_id, token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  could not fetch album {album_id}: {e}")
            failed += 1
            continue
        if not album:
            log.info(f"  album {album_id} is no longer on Qobuz — skipping.")
            failed += 1
            continue
        try:
            with staging_lock():
                result = process_album(album, args, allow_force=False,
                                       already_confirmed=True,
                                       upgrade_only=True, token=token)
        except (AuthLost, QobuzUnavailable):
            raise
        except Exception as e:
            log.info(f"  failed: {e}")
            failed += 1
            continue
        _res = (result or {}).get("result")
        if result and result.get("upgrade_unverified"):
            # Imported, but the rebuilt folder couldn't be verified as complete
            # as the original, so the backup was kept. Not a clean upgrade and
            # not a failure — count it apart so the tally stays honest.
            kept += 1
        elif result and result.get("imported") and _res not in (
                _skip | {"upgrade_aborted_backup_failed"}):
            ok += 1
            # Mirror the CLI upgrade walk: if Qobuz delivered below its advertised
            # quality, write the cap marker so scan_artist_for_upgrades stops
            # re-flagging this album as upgradable on every scan.
            try:
                from qobuz_librarian.library.catalog import find_existing_tracks
                from qobuz_librarian.quality.decision import (
                    compare_album_quality,
                    mark_album_capped,
                )
                post_existing, _ = find_existing_tracks(album)
                if post_existing:
                    post_qual = compare_album_quality(post_existing, album)
                    if post_qual["classification"] in ("all_lower", "mixed_below"):
                        mark_album_capped(album.get("id"), album, post_qual)
                        log.info(f"  upgrade incomplete: {post_qual['n_below']} "
                                 f"track(s) still below target — marked capped "
                                 f"(Qobuz partial hi-res).")
            except Exception as _e_cap:
                log.info(f"  post-upgrade cap check failed: {_e_cap}")
        elif _res not in _skip:
            failed += 1
        time.sleep(cfg.ARTIST_API_DELAY)
    if job.cancel_requested:
        job.summary = (f"Stopped early — {ok} upgraded, "
                       f"{len(chosen) - processed} not started.")
        log.info(job.summary)
        return
    msg = f"Finished — upgraded {ok}/{plural(len(chosen), 'album')}."
    if kept:
        msg += (f" {kept} kept the original (upgrade couldn't be verified "
                f"complete — backup retained).")
    job.summary = msg
    log.info(msg)
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} couldn't be upgraded — see the log."


# ── Downsample flow ─────────────────────────────────────────────────────────────

def scan_downsamples(job):
    """Scan the library for FLACs stored above CD rate.

    Local only — the answer comes off disk, so unlike the upgrade scan there's
    no Qobuz lookup and no token. Serial (the per-file read is fast and disk-
    bound; fanning out would just thrash the spindle) with a cancel check and
    per-artist progress.
    """
    from qobuz_librarian.library.downsample import scan_artist_for_downsample

    clear_scan_caches()
    artists = [d for d in list_library_artists()
               if normalize(d.name) not in VA_NORMALIZED]
    if not artists:
        job.summary = ("No artist folders found under MUSIC_ROOT — check that "
                       "QL_MUSIC_DIR points at your library.")
        log.info("No artist folders found under MUSIC_ROOT.")
        log.info("  Check that QL_MUSIC_DIR in your .env points at the right place.")
        return
    hidden = hidden_mod.load()
    log.info(f"Scanning {plural(len(artists), 'artist')} for hi-res files to downsample")
    total = 0
    done = 0
    n = len(artists)
    for ad in artists:
        if job.cancel_requested:
            log.info("Cancelled — stopping scan.")
            break
        done += 1
        name = ad.name
        try:
            cands = scan_artist_for_downsample(ad)
        except Exception as e:
            log.info(f"    skipped {name}: {e}")
            job.push_progress("Scanning for hi-res files", done, n, name, found=total)
            continue
        added = 0
        for c in cands:
            # An album the user chose to keep hi-res shouldn't be re-flagged
            # every scan.
            if hidden_mod.is_hidden(hidden_mod.SCOPE_DOWNSAMPLE, name, c.title, hidden):
                continue
            # Unticked by default — a downsample is irreversible, so nothing is
            # shrunk without an explicit per-album tick.
            job.add_candidate(
                kind="downsample",
                title=c.title,
                artist=name,
                detail=c.detail,
                payload={"album_dir": str(c.album_dir), "est_saving": c.est_saving},
                selected=False,
            )
            total += 1
            added += 1
        hit = {"artist": name, "albums": added} if added else None
        job.push_progress("Scanning for hi-res files", done, n, name,
                          found=total, hit=hit)
        if added:
            log.info(f"  {name} — {plural(added, 'album')} above CD rate")
    if not job.cancel_requested:
        _flag_new_since_last_scan(job, "downsample")
    if job.cancel_requested:
        job.summary = (f"Stopped early — {plural(total, 'album')} found so far."
                       if total else "Stopped before anything turned up.")
    else:
        job.summary = (f"{plural(total, 'album')} stored above CD rate."
                       + _cap_note(job)
                       if total else
                       "No hi-res files — every album is already at CD rate or lower.")
    log.info(job.summary)


def scan_downsamples_for_artist(job, artist_name):
    """Same downsample scan as ``scan_downsamples`` but scoped to one artist.

    Local-only (off-disk read, no token). The Hidden-downsample store is NOT
    consulted — a deliberate per-artist request means "show me everything for
    this artist", same convention as the upgrade and library variants.
    """
    from qobuz_librarian.library.discovery import resolve_artist_dir
    from qobuz_librarian.library.downsample import scan_artist_for_downsample

    clear_scan_caches()
    artist_dir = resolve_artist_dir(artist_name)
    if artist_dir is None:
        job.summary = (f"No library folder for “{artist_name}”. "
                       "Check the spelling, or scan the whole library.")
        log.info(job.summary)
        return
    name = artist_dir.name
    log.info(f"Scanning {name} for hi-res files to downsample")
    job.push_progress("Scanning for hi-res files", 0, 1, name)
    try:
        cands = scan_artist_for_downsample(artist_dir)
    except Exception as e:
        # Set a terminal status + error so submit_scan's wrapper doesn't read the
        # zero-candidate SCANNING job as a clean run and report "Nothing to do".
        from qobuz_librarian.web.jobs import JobStatus
        log.info(f"  scan failed for {name}: {e}")
        job.error = f"Scan failed: {e}"
        job.summary = "Scan failed — see the log."
        job.status = JobStatus.FAILED
        return
    added = 0
    for c in cands:
        job.add_candidate(
            kind="downsample",
            title=c.title,
            artist=name,
            detail=c.detail,
            payload={"album_dir": str(c.album_dir), "est_saving": c.est_saving},
            selected=False,
        )
        added += 1
    job.push_progress("Scanning for hi-res files", 1, 1, name, found=added)
    log.info(f"  {name} — {plural(added, 'album')} above CD rate")
    if not added:
        job.summary = f"Nothing above CD rate for {name}."


def execute_downsamples(job, chosen):
    """Shrink the chosen albums' hi-res FLACs to CD rate, in place.

    Each file is decode-verified before it overwrites the original (in
    resample_one), so a bad encode can't destroy a master that has no
    re-download fallback.
    """
    from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE, downsample_dir
    from qobuz_librarian.web.jobs import staging_lock

    if not HAVE_DOWNSAMPLE:
        job.error = "Downsampling isn't available on this server."
        return
    shrunk = 0
    total_saved = 0
    total_errors = 0
    skipped = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
        processed = i
        album_dir = Path((cand.get("payload") or {}).get("album_dir", ""))
        title = cand.get("title") or album_dir.name
        log.info(f"[{i}/{len(chosen)}] {cand.get('artist', '')} — {title}")
        if not album_dir.is_dir():
            log.info("  skipped: folder no longer exists")
            skipped += 1
            continue
        try:
            with staging_lock():
                res = downsample_dir(album_dir, verbose=True,
                                     base_dir=album_dir, log=log.info)
        except Exception as e:
            log.info(f"  failed: {e}")
            total_errors += 1
            continue
        if res.get("resampled"):
            shrunk += 1
        total_saved += res.get("saved_bytes", 0)
        total_errors += res.get("errors", 0)
    if job.cancel_requested:
        job.summary = (f"Stopped early — shrank {plural(shrunk, 'album')} "
                       f"({format_size(total_saved)} reclaimed), "
                       f"{len(chosen) - processed} not started.")
        log.info(job.summary)
        return
    summary = (f"Finished — shrank {plural(shrunk, 'album')}, "
               f"reclaimed {format_size(total_saved)}.")
    if skipped:
        summary += f" {plural(skipped, 'album')} skipped (no longer on disk)."
    job.summary = summary
    log.info(summary)
    if total_errors:
        job.error = (f"{plural(total_errors, 'file')} couldn't be downsampled "
                     "(left unchanged) — see the log.")


# ── Repair flow ───────────────────────────────────────────────────────────────

def _repair_album_outcome(album_dir, name, token):
    """Scan one album → a cacheable outcome dict: counts, review-candidate specs,
    and any log lines to emit. AuthLost / QobuzUnavailable propagate (they stop
    the sweep); any other scan error is recorded as a failed album and marked
    not-cacheable so a re-scan retries it rather than remembering the miss."""
    from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs
    out = {"verified_ok": 0, "unverified": 0, "failed": 0, "specs": [],
           "warns": [], "cacheable": True}
    try:
        scan = scan_dir_for_isrc_repairs(album_dir, token, deep=True)
    except (AuthLost, QobuzUnavailable):
        raise
    except Exception as e:
        out["warns"].append(f"    skipped {album_dir.name}: {e}")
        out["failed"] = 1
        out["cacheable"] = False
        return out
    out["verified_ok"] = scan["verified_ok"]
    out["unverified"] = scan.get("unverified", 0)
    truncated = scan["verified_truncated"]
    if truncated:
        out["specs"].append({
            "kind": "repair", "title": album_dir.name, "artist": name,
            "detail": f"{plural(len(truncated), 'truncated track')}",
            "payload": {"album_dir": str(album_dir), "artist_name": name,
                        "verified_truncated": truncated}})
    # Damaged files with no readable ISRC can't be surgically refilled — offer a
    # whole-album re-download instead (the user confirms it in review).
    suspicious = [e for e in scan.get("no_isrc_tag", []) if e.get("diagnostic")]
    if suspicious:
        matched = find_qobuz_album_for_dir(album_dir, name, token)
        if matched and matched.get("id"):
            m_title = matched.get("title") or album_dir.name
            m_year = album_year(matched) or "?"
            out["specs"].append({
                "kind": "redownload", "title": album_dir.name, "artist": name,
                "detail": (f"{plural(len(suspicious), 'damaged file')} can't be "
                           f"verified by ID — re-download the whole album fresh "
                           f"as “{m_title}” ({m_year})"),
                "payload": {"album_dir": str(album_dir), "artist_name": name,
                            "album_id": matched.get("id"),
                            "matched_title": m_title}})
        else:
            for e in suspicious:
                out["warns"].append(
                    f"    ⚠ {album_dir.name} — {e.get('title') or '?'}: "
                    f"{e['diagnostic']}; couldn't match this folder to a Qobuz "
                    "album to re-download — check by hand.")
    return out


def _cached_album_outcome(album_dir, name, token):
    """Per-album repair outcome — served from the signature cache when the album
    is unchanged, computed (and cached) otherwise. Shared by the sweep and the
    post-download check so both reuse the same cached verification."""
    sig = repair_cache.signature(album_dir)
    outcome = repair_cache.get(album_dir, sig)
    if outcome is None:
        outcome = _repair_album_outcome(album_dir, name, token)
        if outcome.pop("cacheable", False):
            repair_cache.put(album_dir, sig, outcome)
    return outcome


def _scan_repair_artist(artist_dir, token, job):
    """Scan one artist's albums for damaged FLACs — runs on a pool worker.

    Returns ``(name, agg)``; ``agg`` carries per-artist counts and a list of
    review-candidate specs the caller adds on the single writer thread, so the
    candidate list and checkpoint stay single-writer (mirroring the library
    scan). Each album's result is cached by its audio-file signature, so a
    re-scan re-checks only changed albums. Bails between albums on cancel;
    AuthLost / QobuzUnavailable propagate so the caller can stop the sweep."""
    name = artist_dir.name
    agg = {"verified_ok": 0, "unverified": 0, "failed": 0, "checked": 0,
           "specs": []}
    for album_dir in list_artist_album_dirs(artist_dir):
        if job.cancel_requested:
            break
        outcome = _cached_album_outcome(album_dir, name, token)
        agg["verified_ok"] += outcome.get("verified_ok", 0)
        agg["unverified"] += outcome.get("unverified", 0)
        agg["failed"] += outcome.get("failed", 0)
        agg["checked"] += 1
        agg["specs"].extend(outcome.get("specs", []))
        for w in outcome.get("warns", []):
            log.info(w)
    return name, agg


def scan_repairs(job, token):
    """Scan every album for ISRC-verified truncated FLACs (fanned out across
    ARTIST_SCAN_WORKERS; see _scan_repair_artist for the per-artist work)."""
    clear_scan_caches()
    artists = list_library_artists()
    if not artists:
        job.summary = ("No artist folders found under MUSIC_ROOT — check that "
                       "QL_MUSIC_DIR points at your library.")
        log.info("No artist folders found under MUSIC_ROOT.")
        log.info("  Check that QL_MUSIC_DIR in your .env points at the right place.")
        return
    # Resume an interrupted sweep: skip the artists already checked and restore
    # the damaged albums they turned up. A repair scan is one Qobuz call per
    # track and runs for hours on a big library, so a container restart or power
    # loss mid-sweep must continue rather than re-check everything from the top.
    cp = scan_checkpoint.load("repair")
    scanned = set(cp["scanned"]) if cp else set()
    total = 0
    n_verified = 0      # ISRC'd FLACs that actually decoded clean this run
    n_unverified = 0    # couldn't decode-check (flac tool absent)
    n_failed = 0        # albums that errored mid-scan (surfaced, not hidden)
    if cp:
        for c in cp["candidates"]:
            _readd_candidate(job, c)
            total += 1
        log.info(f"Resuming — {len(scanned)} artist(s) already checked, "
                 f"{plural(total, 'album')} flagged so far.")
    log.info(f"Scanning {plural(len(artists), 'artist')} for damaged files. "
             "Only problems are listed below — healthy albums stay quiet, so a "
             "long silent stretch is normal; the scan is still working. This "
             "takes a while on a big library.")
    todo = [ad for ad in artists if ad.name not in scanned]
    n = len(artists)
    done = len(scanned)
    since_save = 0
    checked_albums = 0
    last_beat = time.time()
    # Show the progress bar immediately rather than a blank header until the
    # first artist comes back.
    job.push_progress("Checking for damaged files", done, n, "starting…",
                      found=total)
    workers = max(1, int(cfg.ARTIST_SCAN_WORKERS))
    # Scan artists in parallel (each worker gets its own HTTP session), but add
    # candidates, advance progress, and write the checkpoint on THIS one thread
    # so they stay single-writer — the same shape the library scan uses. A repair
    # scan makes a Qobuz call per track, so fanning out is what turns a multi-hour
    # sweep into something watchable.
    from qobuz_librarian.web.jobs import pool_initializer_kwargs
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="repairscan",
                            **pool_initializer_kwargs()) as ex:
        futures = {ex.submit(_scan_repair_artist, ad, token, job): ad
                   for ad in todo}
        for fut in as_completed(futures):
            if job.cancel_requested:
                for f in futures:
                    f.cancel()
                job.summary = (f"Stopped early — {plural(total, 'album')} flagged so far."
                               if total else "Stopped before anything was flagged.")
                log.info("Cancelled — stopping scan.")
                scan_checkpoint.clear("repair")
                return
            done += 1
            try:
                name, agg = fut.result()
            except (AuthLost, QobuzUnavailable):
                # A lost token or an unreachable API isn't a per-artist hiccup —
                # stop the sweep rather than report a partial library as whole.
                # The checkpoint stays, so it resumes once auth/network is back.
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                # A per-artist failure (not auth/outage) is left unscanned so a
                # resume retries it rather than baking in a transient miss.
                log.info(f"    skipped {futures[fut].name}: {e}")
                n_failed += 1
                job.push_progress("Checking for damaged files", done, n,
                                  futures[fut].name, found=total)
                continue
            n_verified += agg["verified_ok"]
            n_unverified += agg["unverified"]
            n_failed += agg["failed"]
            checked_albums += agg["checked"]
            for spec in agg["specs"]:
                job.add_candidate(**spec)
                total += 1
            scanned.add(name)
            job.push_progress("Checking for damaged files", done, n, name,
                              found=total)
            now = time.time()
            if now - last_beat >= _REPAIR_HEARTBEAT_SECS:
                last_beat = now
                log.info(f"  …still scanning — checked {checked_albums:,} albums "
                         f"across {done:,}/{n:,} artists; "
                         f"{plural(total, 'album')} flagged so far.")
            since_save += 1
            if since_save >= _CHECKPOINT_EVERY:
                since_save = 0
                scan_checkpoint.save("repair", scanned, job.candidates, {})
    scan_checkpoint.clear("repair")
    # Honest summary: report what was actually decode-verified, and never claim
    # completeness the scan didn't earn. Surface the un-checkable (no flac tool)
    # and the albums that errored, instead of folding them into a clean total.
    unver = (f" {plural(n_unverified, 'track')} couldn't be decode-checked "
             "(no flac tool)." if n_unverified else "")
    fail = (f" {plural(n_failed, 'album')} couldn't be scanned — re-run to retry."
            if n_failed else "")
    if total:
        job.summary = (f"{plural(total, 'album')} flagged with damaged files. "
                       f"{plural(n_verified, 'track')} decode-verified clean."
                       + unver + fail)
    else:
        job.summary = (f"No damaged files found — "
                       f"{plural(n_verified, 'track')} decode-verified intact."
                       + unver + fail)
    log.info(job.summary)


def scan_repairs_for_artist(job, artist_name, token):
    """Same repair scan as ``scan_repairs`` but scoped to one artist's albums.

    A focused single-artist sweep so no checkpointing — the whole-library run
    needs it because it goes for hours. deep=True is used here so every track
    is verified against Qobuz rather than only the ones that look byte-short;
    for a single artist that is acceptably fast. ISRC misses get the same
    whole-album redownload fallback the library scan does.
    """
    from qobuz_librarian.library.discovery import resolve_artist_dir
    from qobuz_librarian.repair_log import scan_dir_for_isrc_repairs

    clear_scan_caches()
    artist_dir = resolve_artist_dir(artist_name)
    if artist_dir is None:
        job.summary = (f"No library folder for “{artist_name}”. "
                       "Check the spelling, or scan the whole library.")
        log.info(job.summary)
        return
    name = artist_dir.name
    album_dirs = list_artist_album_dirs(artist_dir)
    if not album_dirs:
        job.summary = f"No albums under {name} to check."
        log.info(job.summary)
        return
    log.info(f"Scanning {plural(len(album_dirs), 'album')} under {name}")
    total = 0
    for i, album_dir in enumerate(album_dirs, 1):
        if job.cancel_requested:
            log.info("Cancelled — stopping scan.")
            return
        job.push_progress("Checking for damaged files", i, len(album_dirs),
                          album_dir.name, found=total)
        try:
            scan = scan_dir_for_isrc_repairs(album_dir, token, deep=True)
        except (AuthLost, QobuzUnavailable):
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
    log.info(f"Done. {plural(total, 'album')} flagged.")
    if not total:
        job.summary = f"No damaged files found for {name}."


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
    from qobuz_librarian.modes.process import (
        _upgrade_replacement_verified,
        process_album,
    )
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
    imported_ok = bool(result.get("imported")) and result.get("n_ok", 0) > 0
    if backup:
        if imported_ok and _upgrade_replacement_verified(full, album_dir, backup):
            # The rebuild is verifiably at least as complete as the original
            # (track count + playtime) — safe to drop the backup.
            _shutil.rmtree(backup, ignore_errors=True)
        elif imported_ok:
            # Imported, but a decode pass alone doesn't prove the re-rip kept
            # every track — a truncated or short result could be WORSE than the
            # damaged original it replaced. Keep the only copy that may hold
            # more rather than deleting it on the old decode-only gate.
            log.info("  Re-download landed but couldn't be verified as complete "
                     f"as the original — keeping your backup at {backup}.")
        else:
            log.info("  Re-download didn't complete — restoring the original "
                     "album folder.")
            restore_upgrade_backup(backup, album_dir)
    return result


def execute_repairs(job, chosen, token):
    """Refill ISRC-verified truncated tracks, or re-download whole albums
    whose damage couldn't be ID-verified — depending on each candidate."""
    from qobuz_librarian.modes.repair import repair_album_dir
    from qobuz_librarian.web.jobs import staging_lock

    clear_scan_caches()
    args = build_args()
    fixed = 0
    failed = 0
    processed = 0
    for i, cand in enumerate(chosen, 1):
        if job.cancel_requested:
            break
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
        except (AuthLost, QobuzUnavailable):
            raise
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
    if job.cancel_requested:
        job.summary = (f"Stopped early — {fixed} repaired, "
                       f"{len(chosen) - processed} not started.")
        log.info(job.summary)
        return
    job.summary = f"Finished — repaired {fixed}/{plural(len(chosen), 'album')}."
    log.info(job.summary)
    if failed:
        job.error = f"{failed} of {plural(len(chosen), 'album')} couldn't be repaired — see the log."


def run_lyric_retry(job, token):
    """Retry lyric fetching for tracks queued from a previous failed run."""
    from qobuz_librarian.integrations.lyrics import (
        _refresh_lyric_retry,
        load_lyric_retry,
        lyric_fetch,
        save_lyric_retry,
    )

    paths = load_lyric_retry()
    if not paths:
        job.summary = "No tracks were queued for lyric retry."
        log.info(job.summary)
        return

    if not lyric_fetch.AVAILABLE:
        job.summary = ("The syncedlyrics library isn't installed — manifest "
                       "preserved for a later retry.")
        log.info(job.summary)
        return

    existing = [Path(p) for p in paths if Path(p).exists()]
    dropped = len(paths) - len(existing)
    if dropped:
        log.info(f"{dropped} queued path(s) no longer on disk — skipping.")
    if not existing:
        save_lyric_retry([])
        job.summary = "All queued files are gone from disk; manifest cleared."
        log.info(job.summary)
        return

    log.info(f"Retrying lyrics on {plural(len(existing), 'track')} ...")
    # Hold the staging lock: fetch_for_paths rewrites library FLACs in place, so
    # it must not run concurrently with the scan-lane downsample/repair/upgrade
    # work that mutates the same files (the documented file-mutation mutex).
    from qobuz_librarian.web.jobs import staging_lock
    try:
        with staging_lock():
            lyric_fetch.fetch_for_paths(
                existing, log=log,
                providers=cfg.LYRICS_PROVIDERS or None,
                lyrics_format=cfg.LYRICS_FORMAT,
                state_path=cfg.LYRIC_FETCH_STATE_FILE,
                should_stop=lambda: job.cancel_requested,
            )
    except Exception as e:
        job.error = f"Lyric retry failed: {e} — manifest preserved."
        job.summary = "Lyric retry failed — manifest preserved, will retry next time."
        log.info(job.error)
        return

    _refresh_lyric_retry(existing)
    remaining = load_lyric_retry()
    resolved = len(existing) - len(remaining)
    if job.cancel_requested:
        job.summary = (f"Stopped — resolved {resolved}, "
                       f"{plural(len(remaining), 'track')} still queued for retry.")
    elif remaining:
        job.summary = (f"Resolved {resolved} — {plural(len(remaining), 'track')} "
                       "still unresolved, will retry next time.")
    else:
        job.summary = f"All {plural(len(existing), 'retried track')} resolved."
    log.info(job.summary)


def run_library_lyrics(job, *, rescan=False, synced_only=False):
    """Fetch lyrics for every library track that's missing them."""
    from qobuz_librarian.library.lyrics import HAVE_LYRICS
    from qobuz_librarian.library.lyrics import run_library_lyrics as engine

    if not HAVE_LYRICS:
        job.summary = "Lyric fetching isn't available — the syncedlyrics library isn't installed."
        log.info(job.summary)
        return

    log.info(f"Fetching lyrics across the library (writing {(cfg.LYRICS_FORMAT or 'embed').lower()}).")
    if rescan:
        log.info("Re-checking every track (ignoring saved state).")
    # Hold the staging lock: the engine rewrites library FLACs in place, which
    # must not race the scan-lane downsample/repair/upgrade work on the same tree.
    from qobuz_librarian.web.jobs import staging_lock
    with staging_lock():
        res = engine(rescan=rescan, synced_only=synced_only,
                     should_stop=lambda: job.cancel_requested, log=log)

    total = res.get("total", 0)
    if not total:
        job.summary = "No FLAC files found in the library."
        log.info(job.summary)
        return
    if res.get("stopped"):
        job.summary = f"Stopped after scanning {plural(total, 'track')}."
        return

    wrote = (res.get("wrote-synced", 0) + res.get("wrote-plain", 0)
             + res.get("dry:wrote-synced", 0) + res.get("dry:wrote-plain", 0))
    not_found = res.get("not-found", 0)
    unavailable = res.get("providers-unavailable", 0)
    parts = [f"{plural(total, 'track')} scanned", f"{wrote} got lyrics"]
    if not_found:
        parts.append(f"{not_found} not found")
    if unavailable:
        parts.append(f"{unavailable} couldn't reach a provider (re-run later)")
    job.summary = " · ".join(parts) + "."
    log.info(job.summary)


def run_lyrics_for_artist(job, artist_name, *, rescan=False, synced_only=False):
    """Same lyric backfill as ``run_library_lyrics`` but scoped to one artist.

    Walks only that artist's albums (the engine's iter_library_flacs takes the
    list now). Shares the same state file, so a per-artist run still skips
    tracks an earlier whole-library run already resolved.
    """
    from qobuz_librarian.library.discovery import resolve_artist_dir
    from qobuz_librarian.library.lyrics import HAVE_LYRICS
    from qobuz_librarian.library.lyrics import run_library_lyrics as engine

    if not HAVE_LYRICS:
        job.summary = "Lyric fetching isn't available — the syncedlyrics library isn't installed."
        log.info(job.summary)
        return
    artist_dir = resolve_artist_dir(artist_name)
    if artist_dir is None:
        job.summary = (f"No library folder for “{artist_name}”. "
                       "Check the spelling, or run the whole-library backfill.")
        log.info(job.summary)
        return
    name = artist_dir.name
    log.info(f"Fetching lyrics for {name} "
             f"(writing {(cfg.LYRICS_FORMAT or 'embed').lower()}).")
    if rescan:
        log.info("Re-checking every track (ignoring saved state).")
    # Hold the staging lock: the engine rewrites library FLACs in place, which
    # must not race the scan-lane downsample/repair/upgrade work on the same tree.
    from qobuz_librarian.web.jobs import staging_lock
    with staging_lock():
        res = engine(rescan=rescan, synced_only=synced_only,
                     should_stop=lambda: job.cancel_requested, log=log,
                     artist_dirs=[artist_dir])

    total = res.get("total", 0)
    if not total:
        job.summary = f"No FLAC files found for {name}."
        log.info(job.summary)
        return
    if res.get("stopped"):
        job.summary = f"Stopped after scanning {plural(total, 'track')}."
        return

    wrote = (res.get("wrote-synced", 0) + res.get("wrote-plain", 0)
             + res.get("dry:wrote-synced", 0) + res.get("dry:wrote-plain", 0))
    not_found = res.get("not-found", 0)
    unavailable = res.get("providers-unavailable", 0)
    parts = [f"{plural(total, 'track')} scanned for {name}", f"{wrote} got lyrics"]
    if not_found:
        parts.append(f"{not_found} not found")
    if unavailable:
        parts.append(f"{unavailable} couldn't reach a provider (re-run later)")
    job.summary = " · ".join(parts) + "."
    log.info(job.summary)


# ── Library migration ──────────────────────────────────────────────────────────

def scan_migration(job, src, dest, *, use_acoustid, in_place=False):
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
        n = len(items) if items else 0
        job.summary = (f"Stopped early — {plural(n, 'file')} scanned so far."
                       if n else "Stopped before anything was scanned.")
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
    verb = "move" if in_place else "copy"
    parts = [f"{plural(s['place'], 'file')} ready to {verb}"]
    if s["unplaceable"]:
        parts.append(f"{s['unplaceable']} couldn't be identified")
    if s["collision"]:
        parts.append(f"{s['collision']} skipped to avoid name collisions")
    need, free = engine.space_estimate(plan, in_place=in_place)
    if need and free is not None:
        space = f"≈{format_size(need)} to {verb}, {format_size(free)} free at the destination"
        if need > free:
            space = ("⚠ not enough free space — needs "
                     f"≈{format_size(need)} but only {format_size(free)} is free")
        parts.append(space)
    job.summary = ("; ".join(parts) + ". Unidentified and skipped files stay "
                   f"where they are. Full plan written to {manifest}.")
    log.info(job.summary)


def execute_migration(job, chosen, dest, *, in_place, src=None):
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
    # Serialize the file moves under the staging lock like every other execute
    # flow. Without it a migration writing into the library tree could interleave
    # with a concurrent download lane importing into the same <artist>/<album>
    # path — the exact race the "staging_lock serializes everything that touches
    # the tree" model is meant to prevent.
    from qobuz_librarian.web.jobs import staging_lock
    with staging_lock():
        result = engine.execute_plan(
            plan, in_place=in_place,
            cancel_check=lambda: job.cancel_requested,
            progress=job.push_progress)
        # In-place leaves the emptied source folders behind; clear the husk.
        pruned = engine.prune_empty_dirs(src) if (in_place and src) else 0
    # Leave the preview manifest (the full plan, including what was left behind)
    # alone; record what this run actually did in a sibling results file.
    try:
        engine.write_results_manifest(result, dest / "migration-results.csv")
    except OSError:
        pass
    for failed_src, reason in result.failures[:50]:
        job.push_line(f"failed: {failed_src} — {reason}")

    verb = "moved" if in_place else "copied"
    parts = [f"{plural(result.copied, 'file')} {verb} into {dest}"]
    if result.skipped:
        parts.append(f"{result.skipped} skipped (already present)")
    if result.lingered:
        parts.append(f"{result.lingered} moved but the original couldn't be removed")
    if result.failed:
        parts.append(f"{result.failed} failed — see the log")
        # Set job.error too (not just the prose summary) so a migration with
        # failed copies ends red, like every other execute path, instead of a
        # green DONE that buries "N failed" mid-sentence.
        job.error = f"{plural(result.failed, 'file')} couldn't be migrated — see the log."
    if pruned:
        parts.append(f"cleared {plural(pruned, 'empty source folder')}")
    if result.cancelled:
        parts.append("stopped early")
    job.summary = "; ".join(parts) + "."
    log.info(job.summary)
