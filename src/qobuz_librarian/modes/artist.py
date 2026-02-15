"""Artist mode — two-step run for one artist.

  Step 1 — Gap fill: walk every album you own by this artist and offer to
           download the missing tracks of each.
  Step 2 — Missing albums: list everything on Qobuz by this artist you
           don't have, and offer to download.
"""
import re
import shutil
import time

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzError
from qobuz_librarian.api.search import get_album, get_artist_albums, search_artists
from qobuz_librarian.library.catalog import (
    _count_audio_files_in,
    album_quality_label,
    album_year,
    compute_missing,
    dedup_album_versions,
    filter_compilation_albums,
    filter_owned_albums,
    filter_seen_album_ids,
    filter_short_releases,
    find_album_dir_filesystem,
    find_existing_tracks,
    find_expanded_edition,
    find_extras_in_existing,
    find_qobuz_album_for_dir,
    is_lossless_album,
    match_dir_to_catalog,
)
from qobuz_librarian.library.scanner import (
    clear_scan_caches,
    list_artist_album_dirs,
)
from qobuz_librarian.library.tags import (
    VA_NORMALIZED,
    normalize,
    similarity,
    strip_album_decorations,
)
from qobuz_librarian.modes.process import (
    detect_sibling_album_groups,
    pick_canonical_sibling,
    process_album,
)
from qobuz_librarian.quality.decision import compare_album_quality
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.queue.executor import _execute_download_queue
from qobuz_librarian.ui_cli.colors import C, banner, fmt, section, truncate
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import (
    _flush_stdin,
    confirm,
    parse_number_list,
    prompt_edition_pick,
)


def resolve_artist_dir(artist_query):
    """Fuzzy-find an artist's directory in MUSIC_ROOT.
    Returns Path or None. Handles 'The X' / 'X' equivalence."""
    from qobuz_librarian.library.scanner import list_library_artists
    if not artist_query:
        return None
    candidates = list_library_artists()
    if not candidates:
        return None

    target = normalize(artist_query)
    if not target:
        return None

    # Exact normalized match
    for d in candidates:
        if normalize(d.name) == target:
            return d

    # 'The X' / 'X' equivalence
    target_alt = target[3:] if target.startswith("the") else "the" + target
    for d in candidates:
        if normalize(d.name) == target_alt:
            return d

    # Fuzzy fallback (try both raw and 'the'-stripped)
    def base(s):
        return s[4:] if s.lower().startswith("the ") else s
    best, best_score = None, 0.0
    for d in candidates:
        s = max(similarity(d.name, artist_query),
                similarity(base(d.name), base(artist_query)))
        if s > best_score:
            best, best_score = d, s
    if best and best_score >= cfg.FUZZY_DIR_THRESH:
        return best
    return None


def run_artist_gap_fill(artist_name, artist_dir, args, token, *,
                      shared_queue=None, flush_callback=None,
                      skip_predicate=None, save_callback=None):
    """Walk every album dir under artist_dir, query Qobuz, prompt to fill gaps.

    Returns (results, owned_bare_titles, seen_album_ids, seed_artist_id, catalog).
    seed_artist_id is the Qobuz artist_id captured from the first successful
    per-album match; the missing-albums step uses it to skip a redundant
    artist/search. seen_album_ids is the set of Qobuz album IDs touched in
    gap-fill — the missing-albums step uses it to filter so an upgraded
    album can't reappear as "missing".

    flush_callback (optional, used by album fill walk): a zero-arg callable. When supplied,
    the per-album prompt gains a 'd' option that calls flush_callback() to
    download whatever is currently in shared_queue, then resumes scanning
    the next album. shared_queue must also be supplied for this to do
    anything useful — flush_callback is responsible for clearing it.

    skip_predicate (optional, used by album fill walk): callable taking an album Path,
    returning True to skip that album completely (no Qobuz query, no
    prompt, no result append). Album fill walk uses this to honour its per-album
    seen file so previously-decided albums aren't re-walked when the
    artist has mixed seen/unseen state.

    save_callback (optional, modes 4 & 5): zero-arg callable invoked
    after this artist's queue items have been added to shared_queue,
    so the caller can persist the updated queue to disk for crash
    recovery. Cheap, but only fires once per artist (not per album).
    """
    album_dirs = list_artist_album_dirs(artist_dir)
    if not album_dirs:
        log.info(fmt(C.YELLOW, f"  ⚠  No album folders found under {artist_dir}."))
        return [], set(), set(), None, []

    # Detect sibling folders (same bare title) before any downloads.
    sibling_choices = {}  # picked_dir -> [siblings to delete after fill]

    def _maybe_delete_siblings(picked, r=None):
        if picked not in sibling_choices:
            return
        ok = (r is None
              or r.get("result") in ("already_complete",
                                     "skipped_already_higher_quality")
              or (r.get("n_ok", 0) > 0 and r.get("n_fail", 0) == 0
                  and r.get("imported", False)))
        if not ok:
            return
        for sib in sibling_choices[picked]:
            if not sib.exists():
                continue
            try:
                shutil.rmtree(sib)
                log.info(fmt(C.GRAY, f"    🗑  Removed sibling: {sib.name}."))
            except OSError as e:
                log.info(fmt(C.YELLOW,
                    f"    ⚠  Couldn't remove {sib.name}: {e}."))
        sibling_choices[picked] = []

    # Catalog pre-fetch moved here so quality labels are available
    # when the user picks between sibling folders.
    artist_id = None
    catalog = []
    vlog("  Resolving artist + pre-fetching catalog …")
    try:
        artists = search_artists(artist_name, token, limit=cfg.ARTIST_LOOKUP_LIMIT)
    except QobuzError as e:
        log.info(fmt(C.YELLOW, f"  ⚠  artist/search failed ({e}); per-folder fallback."))
        artists = []
    if artists:
        best = max(artists, key=lambda a: similarity(a.get("name", ""), artist_name))
        if similarity(best.get("name", ""), artist_name) >= cfg.ARTIST_NAME_THRESH:
            artist_id = best.get("id")
            try:
                catalog, qobuz_total = get_artist_albums(
                    artist_id, token, limit=cfg.ARTIST_CATALOG_LIMIT)
                vlog(f"  Pre-fetched {len(catalog)} catalog entries (artist_id={artist_id}).")
                if qobuz_total is not None and qobuz_total > len(catalog):
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Qobuz reports {qobuz_total} albums; only fetched "
                        f"{len(catalog)}. Folders past the limit will fall back to search."))
            except QobuzError as e:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  catalog pre-fetch failed ({e}); per-folder fallback."))
                catalog = []
        else:
            log.info(fmt(C.YELLOW,
                f"  ⚠  best Qobuz artist match {best.get('name')!r} doesn't match "
                f"{artist_name!r} strongly; per-folder fallback."))

    sibling_groups = detect_sibling_album_groups(album_dirs)
    if sibling_groups:
        log.info(fmt(C.YELLOW,
            f"\n  ⚠  Detected {len(sibling_groups)} sibling folder group(s):"))
        skip_set = set()
        for bare, dirs in sibling_groups:
            print()
            log.info(fmt(C.WHITE, f"  Group '{bare}':"))
            for j, d in enumerate(dirs, 1):
                n_audio = _count_audio_files_in(d)
                _sib_qual = ""
                if catalog:
                    _sc = match_dir_to_catalog(d, catalog, artist_name)
                    if _sc:
                        _sib_qual = f"  {fmt(C.CYAN, album_quality_label(_sc))}"
                log.info(f"    {fmt(C.BOLD, str(j))}. "
                         f"{fmt(C.WHITE, d.name)}  "
                         f"{fmt(C.GRAY, f'[{n_audio} audio file(s)]')}"
                         f"{_sib_qual}")
            log.info(fmt(C.GRAY,
                "    Pick which to keep — others deleted on successful fill."))
            log.info(fmt(C.GRAY, "    Enter to skip the entire group."))
            picked = None
            yes_skip_delete = False  # --yes picks but never deletes
            if args.yes:
                picked = pick_canonical_sibling(dirs)
                yes_skip_delete = True
                log.info(fmt(C.GRAY,
                    f"    --yes: picking {picked.name} "
                    f"(siblings preserved; deletion needs interactive confirm)"))
            else:
                while True:
                    try:
                        rr = input(fmt(C.CYAN,
                            f"    Pick [1-{len(dirs)}, enter=skip]: "
                        )).strip()
                    except EOFError:
                        rr = ""
                    if not rr:
                        break
                    if rr.isdigit() and 1 <= int(rr) <= len(dirs):
                        picked = dirs[int(rr) - 1]
                        break
                    log.info(fmt(C.GRAY,
                        f"    Enter 1-{len(dirs)} or blank to skip."))
            if picked is None:
                log.info(fmt(C.GRAY, "    Skipped entire group."))
                for d in dirs:
                    skip_set.add(d)
                continue
            others = [d for d in dirs if d is not picked]
            # Under --yes, pick but DO NOT enqueue siblings for
            # delete-after-fill; matches docstring's --yes contract.
            sibling_choices[picked] = [] if yes_skip_delete else others
            for d in others:
                skip_set.add(d)
        if skip_set:
            album_dirs = [d for d in album_dirs if d not in skip_set]
            log.info(fmt(C.GRAY,
                f"  → {len(album_dirs)} folder(s) to scan."))

    # Pre-populate owned_bare_titles from ALL album dirs upfront.
    # owned_bare_titles: {normalized_bare_title: set_of_int_years}.
    owned_bare_titles = {}
    for _ad in album_dirs:
        _tkey = normalize(strip_album_decorations(_ad.name))
        if not _tkey:
            continue
        _ym = re.search(r"\((\d{4})\)", _ad.name) or re.search(r"\b(19\d{2}|20\d{2})\b", _ad.name)
        _yr = int(_ym.group(1)) if _ym else None
        owned_bare_titles.setdefault(_tkey, set()).add(_yr)
    seen_album_ids = set()  # Qobuz IDs touched in gap-fill; excluded from missing-albums step

    section(f"Step 1: Gap fill — {len(album_dirs)} album(s) for {artist_name}")
    vlog("  For each, querying Qobuz and offering to fill missing tracks.")
    vlog("  Press 's' at any prompt to stop the scan.")

    results = []
    seed_artist_id = artist_id
    stopped_early = False
    # Typing 'a' at any gap-fill prompt auto-confirms the rest
    # of THIS artist's prompts. Scoped local — does not bleed into step 2
    # or the next artist in a walk.
    auto_yes_rest = False
    # Collect download decisions here, run them as a batch
    # AFTER the prompting loop completes (so user can walk away).
    queue = []

    # If AuthLost fires mid-loop, stash it here, finish
    # the post-loop hand-off so the artist's accumulated `queue` items
    # reach shared_queue, then re-raise.
    pending_auth_lost = None

    for i, ad in enumerate(album_dirs, 1):
        if stopped_early:
            break

        # Album fill walk: skip albums already in the album-walk seen file.
        if skip_predicate is not None:
            try:
                if skip_predicate(ad):
                    continue
            except Exception as _spe:
                vlog(f"  skip_predicate raised on {ad.name}: {_spe}")

        label = f"[{i}/{len(album_dirs)}]"
        print()
        log.info(fmt(C.BOLD + C.WHITE, f"  {label} {truncate(ad.name, 55)}"))

        try:
            album = find_qobuz_album_for_dir(ad, artist_name, token,
                                             prefer_hires=args.prefer_hires,
                                             catalog=catalog,
                                             target_dir=ad)
        except AuthLost as _e_auth:
            pending_auth_lost = _e_auth
            break
        time.sleep(cfg.ARTIST_API_DELAY)

        # Sibling-pick fallback. Picked folder failed to match —
        # prompt to swap in each alternate from its sibling group in turn.
        fallback_ran = False
        if album is None and ad in sibling_choices:
            fallback_ran = True
            log.info(fmt(C.YELLOW,
                f"    ⚠  No Qobuz match for {ad.name}."))
            fallback_queue = list(sibling_choices[ad])
            original_ad = ad
            while fallback_queue:
                next_try = fallback_queue.pop(0)
                if not confirm(
                        f"    Try sibling {next_try.name} instead?",
                        default_yes=True, auto_yes=args.yes):
                    log.info(fmt(C.GRAY,
                        "    Skipping the rest of this group."))
                    break
                log.info(fmt(C.GRAY, f"    Trying {next_try.name} …"))
                try:
                    album = find_qobuz_album_for_dir(
                        next_try, artist_name, token,
                        prefer_hires=args.prefer_hires,
                        catalog=catalog,
                        target_dir=next_try)
                except AuthLost as _e_auth_sib:
                    pending_auth_lost = _e_auth_sib
                    album = None
                    break
                time.sleep(cfg.ARTIST_API_DELAY)
                if album is not None:
                    all_in_group = (sibling_choices[original_ad]
                                    + [original_ad])
                    new_others = [d for d in all_in_group
                                  if d is not next_try]
                    sibling_choices[next_try] = new_others
                    if original_ad in sibling_choices:
                        del sibling_choices[original_ad]
                    ad = next_try
                    log.info(fmt(C.GREEN,
                        f"    ✓  Switched to {ad.name}."))
                    break
                log.info(fmt(C.YELLOW,
                    f"    ⚠  No Qobuz match for {next_try.name} either."))

        if pending_auth_lost is not None:
            break

        if album is None:
            if not fallback_ran:
                log.info(fmt(C.YELLOW, "    ⚠  No confident Qobuz match for this folder. Skipping."))
            results.append({"dir": ad, "result": "no_qobuz_match"})
            continue

        # GUARDRAIL: verify the matched Qobuz album's predicted on-disk path
        # actually points back at THIS dir.
        from qobuz_librarian.library.catalog import _paths_equal
        predicted = find_album_dir_filesystem(album)
        if predicted is None or not _paths_equal(predicted, ad):
            pred_name = predicted.name if predicted else "no on-disk match"
            log.info(fmt(C.YELLOW,
                f"    ⚠  Qobuz match resolves to a different folder ({pred_name}). "
                f"Likely false match — skipping to avoid duplication."))
            results.append({"dir": ad, "result": "predicted_path_mismatch",
                            "qobuz_title": album.get("title") or "?"})
            continue

        # Capture artist_id for the missing-albums step
        if seed_artist_id is None:
            seed_artist_id = ((album.get("artist") or {}).get("id"))

        # Mark this Qobuz album's normalized bare title as owned.
        _tkey = normalize(strip_album_decorations(album.get("title", "")))
        if _tkey:
            _yr_str = album_year(album)
            try:
                _yr = int(_yr_str) if _yr_str else None
            except ValueError:
                _yr = None
            owned_bare_titles.setdefault(_tkey, set()).add(_yr)
        if album.get("id") is not None:
            seen_album_ids.add(album["id"])

        qobuz_tracks = (album.get("tracks") or {}).get("items") or []
        if not qobuz_tracks:
            log.info(fmt(C.YELLOW, "    ⚠  Qobuz returned no tracks. Skipping."))
            results.append({"dir": ad, "result": "no_tracks"})
            continue

        # Quick check: complete AND at-or-above Qobuz quality → short-circuit.
        existing, _ = find_existing_tracks(album)
        missing, present = compute_missing(qobuz_tracks, existing)
        n_total = len(qobuz_tracks)

        # GUARDRAIL: zero-overlap false-match.
        if existing and not present:
            log.info(fmt(C.YELLOW,
                f"    ⚠  Qobuz match has 0 track overlap with this folder "
                f"({len(existing)} on disk, none matched). "
                f"Likely false match — skipping to avoid duplication."))
            results.append({"dir": ad, "result": "false_match",
                            "qobuz_title": album.get("title") or "?",
                            "n_existing": len(existing),
                            "n_qobuz": n_total})
            continue

        extra_albums = []
        has_extras = False
        if existing and not getattr(args, "no_upgrade", False):
            qual = compare_album_quality(existing, album)
            if qual["classification"] in ("all_lower", "mixed_below"):
                extra_albums = find_extras_in_existing(qobuz_tracks, existing)
                if extra_albums:
                    has_extras = True

        # Artist mode: complete is complete. Upgrade prompts removed.
        if not missing:
            if has_extras:
                # Higher quality but bonus tracks would be lost. Look for expanded edition.
                _cands = find_expanded_edition(album, ad, existing, token, args)
                _exp, _exp_extras = prompt_edition_pick(
                    album, len(extra_albums), _cands, existing, args, label_prefix="    ")
                if _exp is not None:
                    log.info(fmt(C.MAGENTA,
                        f"    ↑  Switching to {_exp.get('title') or '?'!r} at "
                        f"{album_quality_label(_exp)} — queued for batch upgrade"))
                    _exp_tracks = (_exp.get("tracks") or {}).get("items") or []
                    queue.append(_build_queue_item(
                        album=_exp, album_dir=ad, label=label,
                        missing=list(_exp_tracks), present=[],
                        upgrade_only=False, auto_upgrade=True,
                        siblings_to_delete=sibling_choices.get(ad, []),
                    ))
                    continue
                log.info(fmt(C.GRAY,
                    f"    ✓  All {n_total} track(s) present "
                    f"(Qobuz higher-quality but you have bonus tracks; preserving)"))
            else:
                log.info(fmt(C.GREEN,
                    f"    ✓  All {n_total} track(s) present — checking next"))
            results.append({"dir": ad, "result": "already_complete", "n_total": n_total})
            _maybe_delete_siblings(ad)
            continue

        # Artist mode is gap-fill only — no upgrade prompts.
        _extras_sfx = f" + {len(extra_albums)} local-only" if has_extras else ""
        log.info(fmt(C.YELLOW,
            f"    {len(present)}/{n_total} present{_extras_sfx} — {len(missing)} missing"))
        for _mt in missing[:8]:
            _tn = _mt.get("track_number") or "?"
            log.info(fmt(C.GRAY,
                f"      {str(_tn).rjust(2)}. {truncate(_mt.get('title') or '?', 56)}"))
        if len(missing) > 8:
            log.info(fmt(C.GRAY, f"      … and {len(missing) - 8} more"))
        _qsr_raw = album.get("maximum_sampling_rate") or 0
        _qsr_khz = (_qsr_raw / 1000) if _qsr_raw >= 1000 else _qsr_raw
        _compress_note = "  → will compress to 48kHz" if _qsr_khz > 48 else ""
        log.info(fmt(C.GRAY,
            f"    Qobuz: {album_year(album) or '?'} • {album_quality_label(album)}{_compress_note}"))

        if args.dry_run:
            log.info(fmt(C.GRAY, "    --dry-run: would prompt to download here"))
            results.append({"dir": ad, "result": "dry_run", "n_missing": len(missing)})
            continue

        if args.yes or auto_yes_rest:
            answer = "y"
        else:
            while True:
                _flush_hint = (
                    f"/d=download queue ({len(shared_queue) if shared_queue else 0})"
                ) if (flush_callback is not None and shared_queue is not None) else ""
                _q = (f"    Download {len(missing)} missing track(s)? "
                      f"[y/N/a=yes-rest{_flush_hint}/s=stop]: ")
                _flush_stdin()
                try:
                    answer = input(fmt(C.CYAN, _q)).strip().lower()
                except EOFError:
                    answer = "n"
                    break
                if answer == "d" and flush_callback is not None:
                    try:
                        flush_callback()
                    except KeyboardInterrupt:
                        log.info(fmt(C.GRAY,
                            "\n    Flush interrupted; back to the prompt."))
                    continue
                if answer == "a":
                    auto_yes_rest = True
                    answer = "y"
                    log.info(fmt(C.GRAY,
                        "    Auto-yes for the rest of this artist's albums."))
                break

        if answer == "s":
            log.info(fmt(C.GRAY, "    Stopping artist scan."))
            stopped_early = True
            results.append({"dir": ad, "result": "user_stopped"})
            break
        if answer not in ("y", "yes"):
            results.append({"dir": ad, "result": "user_skipped", "n_missing": len(missing)})
            continue

        queue.append(_build_queue_item(
            album=album, album_dir=ad, label=label,
            missing=list(missing), present=list(present),
            upgrade_only=False, auto_upgrade=False,
            siblings_to_delete=sibling_choices.get(ad, []),
        ))

    # Shared_queue mode — accumulate into it and let the caller flush.
    if shared_queue is not None:
        shared_queue.extend(queue)
        if save_callback is not None and queue:
            try:
                save_callback()
            except Exception as _sce:
                vlog(f"  save_callback raised: {_sce}")
    elif queue:
        queue_results, _ = _execute_download_queue(queue, args, token)
        for qi, qr in zip(queue, queue_results):
            results.append(qr)
            _maybe_delete_siblings(qi["album_dir"], qr)

    # Re-raise deferred AuthLost after hand-off completes.
    if pending_auth_lost is not None:
        raise pending_auth_lost

    return results, owned_bare_titles, seen_album_ids, seed_artist_id, catalog


def run_artist_missing_albums(artist_name, owned_bare_titles, args, token,
                      seed_artist_id=None, seen_album_ids=None,
                      prefetched_catalog=None, *, shared_queue=None):
    """Show artist's full Qobuz catalog with the user's already-owned albums
    filtered out, dedup multiple editions, prompt to download some.

    Returns count of albums downloaded.
    """
    section(f"Step 2: Missing albums — by {artist_name}, not yet in your library")

    # Resolve artist_id. Prefer a seed captured during gap-fill.
    artist_id = seed_artist_id
    if artist_id is None:
        log.info(fmt(C.GRAY, "  Looking up artist on Qobuz …"))
        try:
            artists = search_artists(artist_name, token, limit=cfg.ARTIST_LOOKUP_LIMIT)
        except QobuzError as e:
            log.info(fmt(C.YELLOW, f"  ⚠  Qobuz artist search failed: {e}."))
            return 0
        if not artists:
            log.info(fmt(C.YELLOW, "  ⚠  Couldn't find this artist on Qobuz."))
            return 0
        best = max(artists, key=lambda a: similarity(a.get("name", ""), artist_name))
        if similarity(best.get("name", ""), artist_name) < cfg.ARTIST_NAME_THRESH:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Best Qobuz artist match is {best.get('name')!r}, "
                f"which doesn't strongly match {artist_name!r}. Skipping step 2."))
            return 0
        artist_id = best.get("id")
        log.info(fmt(C.GRAY,
            f"  Matched Qobuz artist: {best.get('name')!r} (id {artist_id})"))
    else:
        vlog(f"  using seed artist_id={artist_id} from gap-fill")

    if prefetched_catalog:
        catalog = prefetched_catalog
        vlog(f"  Reusing catalog from gap-fill ({len(catalog)} entries) — no refetch.")
    else:
        vlog(f"  Fetching catalog (limit {cfg.ARTIST_CATALOG_LIMIT}) …")
        try:
            catalog, qobuz_total = get_artist_albums(artist_id, token,
                                                     limit=cfg.ARTIST_CATALOG_LIMIT)
        except QobuzError as e:
            log.info(fmt(C.YELLOW, f"  ⚠  Couldn't fetch artist catalog: {e}."))
            return 0
        vlog(f"  Qobuz returned {len(catalog)} catalog entries (pre-dedup)")
        if qobuz_total is not None and qobuz_total > len(catalog):
            log.info(fmt(C.YELLOW,
                f"  ⚠  Qobuz reports {qobuz_total} total albums; only fetched "
                f"{len(catalog)} (limit={cfg.ARTIST_CATALOG_LIMIT}). Some entries omitted."))

    catalog = [a for a in catalog if is_lossless_album(a)]
    pairs = dedup_album_versions(catalog, prefer_hires=args.prefer_hires)
    n_after_dedup = len(pairs)

    if not args.include_comps:
        pairs = filter_compilation_albums(pairs, artist_name)
    n_after_comp = len(pairs)

    if not getattr(args, "include_singles", False):
        pairs = filter_short_releases(pairs, cfg.MISSING_ALBUMS_MIN_TRACKS)
    n_after_singles = len(pairs)

    pairs = filter_seen_album_ids(pairs, seen_album_ids or set())
    n_after_seen = len(pairs)

    pairs = filter_owned_albums(pairs, owned_bare_titles)

    vlog(f"  After dedup: {n_after_dedup} unique; after comp filter: {n_after_comp}; "
         f"after singles filter: {n_after_singles}; after seen: {n_after_seen}; "
         f"after owned filter: {len(pairs)}")

    # Cross-check remaining candidates for partial ownership.
    def _partial_check(album):
        existing, album_dir = find_existing_tracks(album)
        if not existing or album_dir is None:
            return []
        # Year sanity check.
        folder_yr = None
        ym = (re.search(r"\((\d{4})\)", album_dir.name)
              or re.search(r"\b(19\d{2}|20\d{2})\b", album_dir.name))
        if ym:
            try:
                folder_yr = int(ym.group(1))
            except ValueError:
                folder_yr = None
        album_yr_str = album_year(album)
        try:
            album_yr = int(album_yr_str) if album_yr_str else None
        except ValueError:
            album_yr = None
        # 3-year window: an "Album (2005)" folder can legitimately match a
        # 2003 remaster or a 2008 deluxe edition (Qobuz often reports the
        # most recent re-release year). Beyond ±3 years it's almost always
        # a different album that happens to share a title.
        if (folder_yr is not None and album_yr is not None
                and abs(folder_yr - album_yr) > 3):
            return []
        return existing

    pairs_partial = []
    pairs_missing = []
    for album, n_versions in pairs:
        found = _partial_check(album)
        if found:
            n_total = album.get("tracks_count") or "?"
            n_have = len(found)
            if isinstance(n_total, int):
                n_have = min(n_have, n_total)
            pairs_partial.append((album, n_versions, n_have, n_total))
        else:
            pairs_missing.append((album, n_versions))

    n_partial = len(pairs_partial)
    vlog(f"  Partial-ownership check: {n_partial} partial, {len(pairs_missing)} fully missing")

    pairs = [(a, nv) for a, nv, _, _ in pairs_partial] + pairs_missing

    if not pairs:
        log.info(fmt(C.GREEN, "\n  ✓  No new albums to suggest. You're caught up!\n"))
        return 0

    print()
    header = f"  {len(pairs)} album(s) you don't have"
    if n_partial:
        header += f"  ({n_partial} partially present — listed first)"
    log.info(fmt(C.BOLD + C.WHITE, header + ":"))
    print()
    for i, (a, n_versions) in enumerate(pairs, 1):
        title  = a.get("title") or "?"
        year   = album_year(a) or "?"
        tracks = a.get("tracks_count") or "?"
        qual   = album_quality_label(a)
        suffix = f" ({n_versions} editions)" if n_versions > 1 else ""

        line1 = (f"  {fmt(C.BOLD, str(i).rjust(3))}.  "
                 f"{fmt(C.WHITE, truncate(title, 55))}")
        line2 = f"        {fmt(C.GRAY, f'{year} • {tracks} tracks • {qual}')}{fmt(C.GRAY, suffix)}"
        if i - 1 < n_partial:
            _, _, n_have, n_tot = pairs_partial[i - 1]
            line2 += fmt(C.YELLOW, f"  ← {n_have}/{n_tot} tracks already present")
        print(line1)
        print(line2)
    print()

    if args.dry_run:
        log.info(fmt(C.YELLOW, "  --dry-run: stopping here, nothing prompted."))
        return 0

    _flush_stdin()
    try:
        raw = input(fmt(C.CYAN,
            "  Pick numbers to download (e.g. 1,3,5-7 / a=all / blank=skip): ")).strip()
    except EOFError:
        raw = ""
    picks = parse_number_list(raw, len(pairs))
    if not picks:
        return 0

    # Quality is taken from the streamrip config for missing-album
    # downloads; per-call override slot left in place for future use.
    quality_override = None

    # Shared_queue mode — queue instead of processing immediately.
    if shared_queue is not None:
        log.info(fmt(C.GRAY,
            f"  Queuing {plural(len(picks), 'album')}; fetching track lists …"))
        n_queued = 0
        for i, idx in enumerate(picks, 1):
            chosen, _ = pairs[idx - 1]
            try:
                full = get_album(chosen["id"], token)
            except QobuzError as e:
                log.info(fmt(C.YELLOW,
                    f"    ⚠  Couldn't fetch: {e}. Skipping."))
                continue
            full_tracks = (full.get("tracks") or {}).get("items") or []
            shared_queue.append(_build_queue_item(
                album=full, album_dir=None,
                label=f"new {i}/{len(picks)}",
                missing=list(full_tracks), present=[],
                upgrade_only=False, auto_upgrade=False,
                siblings_to_delete=[],
                quality=quality_override,
            ))
            n_queued += 1
            time.sleep(cfg.ARTIST_API_DELAY)
        log.info(fmt(C.GREEN,
            f"  ✓ Queued {plural(n_queued, 'album')} "
            f"(queue total: {len(shared_queue)})."))
        return n_queued

    log.info(fmt(C.GRAY, f"  Selected {plural(len(picks), 'album')}; fetching track lists …"))

    saved_yes = args.yes
    args.yes = True
    n_done = 0
    try:
        for i, idx in enumerate(picks, 1):
            chosen, _ = pairs[idx - 1]
            label = f"[new {i}/{len(picks)}]"
            print()
            log.info(fmt(C.BOLD + C.WHITE, f"  {label} {truncate(chosen.get('title') or '?', 55)}"))
            try:
                full = get_album(chosen["id"], token)
            except QobuzError as e:
                log.info(fmt(C.YELLOW, f"    ⚠  Couldn't fetch: {e}. Skipping."))
                continue
            try:
                r = process_album(full, args, allow_force=False, label=label,
                                  already_confirmed=True, token=token,
                                  quality=quality_override)
            except KeyboardInterrupt:
                log.info(fmt(C.GRAY, "\n    Interrupted. Stopping step 2."))
                break
            if r.get("n_ok", 0) > 0 and r.get("imported", False):
                n_done += 1
            time.sleep(cfg.ARTIST_API_DELAY)
    finally:
        args.yes = saved_yes

    return n_done


def run_artist_mode(artist_name, args, token):
    """Top-level artist mode entry point. Returns nothing; runs both phases."""
    clear_scan_caches()
    banner(f"Artist mode — {artist_name}")

    if normalize(artist_name) in VA_NORMALIZED:
        log.info(fmt(C.YELLOW,
            "\n  ⚠  'Various Artists' isn't a real artist; artist mode doesn't support it."))
        log.info(fmt(C.GRAY,
            "     Use album mode for individual compilation albums."))
        return

    # --consolidate inside artist mode would prompt per album (a LOT on a
    # 30-album scan). Auto-disabled here; saved/restored so a one-shot
    # artist run doesn't bleed back into later album-mode runs.
    saved_consolidate = args.consolidate
    if args.consolidate:
        args.consolidate = False
        vlog("  · Consolidation auto-disabled for artist mode (would prompt per album).")

    try:
        artist_dir = resolve_artist_dir(artist_name)
        if artist_dir is None:
            log.info(fmt(C.YELLOW, f"\n  ⚠  No matching artist directory in {cfg.MUSIC_ROOT}."))
            if confirm("\n  Continue anyway and just look for albums by this artist on Qobuz?",
                       default_yes=False, auto_yes=args.yes):
                run_artist_missing_albums(artist_name, {}, args, token)
            return
        vlog(f"  Library: {artist_dir}")

        gap_fill_results, owned_bare_titles, seen_album_ids, seed_id, prefetched_catalog = run_artist_gap_fill(
            artist_name, artist_dir, args, token)

        n_complete   = sum(1 for r in gap_fill_results if r.get("result") == "already_complete")
        n_filled     = sum(1 for r in gap_fill_results
                           if r.get("n_ok", 0) > 0 and r.get("imported", False))
        n_upgraded   = sum(1 for r in gap_fill_results
                           if r.get("n_ok", 0) > 0 and r.get("imported", False)
                           and r.get("auto_upgrade"))
        n_skipped    = sum(1 for r in gap_fill_results
                           if r.get("result") in ("user_skipped", "user_stopped"))
        n_extras     = sum(1 for r in gap_fill_results
                           if r.get("result") == "skipped_has_extras")
        n_already_hr = sum(1 for r in gap_fill_results
                           if r.get("result") == "skipped_already_higher_quality")
        no_match     = [r for r in gap_fill_results if r.get("result") == "no_qobuz_match"]
        path_mis     = [r for r in gap_fill_results if r.get("result") == "predicted_path_mismatch"]
        false_match  = [r for r in gap_fill_results if r.get("result") == "false_match"]

        section("Step 1: Gap-fill summary")
        print()
        if n_complete:
            log.info(f"  {fmt(C.GREEN,   '✓ already complete:')}    {n_complete}")
        if n_filled:
            log.info(f"  {fmt(C.GREEN,   '✓ tracks filled:')}       {n_filled}")
        if n_upgraded:
            log.info(f"  {fmt(C.MAGENTA, '↑ auto-upgraded:')}        {n_upgraded}")
        if n_already_hr:
            log.info(f"  {fmt(C.GRAY,  '· kept (already hi-res):')}{n_already_hr}")
        if n_extras:
            log.info(f"  {fmt(C.GRAY,  '· skipped (has extras):')} {n_extras}")
        if n_skipped:
            log.info(f"  {fmt(C.YELLOW,  'skipped by user:')}      {n_skipped}")
        if no_match:
            log.info(f"  {fmt(C.YELLOW,  'no Qobuz match:')}       {len(no_match)}")
        if path_mis:
            log.info(f"  {fmt(C.YELLOW,  'path mismatch:')}        {len(path_mis)}")
        if false_match:
            log.info(f"  {fmt(C.YELLOW,  'false match (0 overlap):')}{len(false_match)}")

        if no_match:
            print()
            log.info(fmt(C.YELLOW, "  no Qobuz match — folders to investigate:"))
            for r in no_match[:25]:
                log.info(fmt(C.GRAY, f"    • {r['dir'].name}"))
            if len(no_match) > 25:
                log.info(fmt(C.GRAY, f"    ... and {len(no_match) - 25} more"))
        if path_mis:
            print()
            log.info(fmt(C.YELLOW, "  path mismatch (Qobuz match resolved to a different folder):"))
            for r in path_mis[:25]:
                qt = r.get("qobuz_title", "?")
                log.info(fmt(C.GRAY, f"    • {r['dir'].name}   →   matched {truncate(qt, 40)!r}"))
            if len(path_mis) > 25:
                log.info(fmt(C.GRAY, f"    ... and {len(path_mis) - 25} more"))
        if false_match:
            print()
            log.info(fmt(C.YELLOW, "  false match (Qobuz match shared 0 tracks with folder):"))
            for r in false_match[:25]:
                qt = r.get("qobuz_title", "?")
                log.info(fmt(C.GRAY,
                    f"    • {r['dir'].name}   →   matched {truncate(qt, 40)!r}"))
            if len(false_match) > 25:
                log.info(fmt(C.GRAY, f"    ... and {len(false_match) - 25} more"))

        if args.no_catalog:
            log.info(fmt(C.GRAY, "\n  --no-catalog: skipping step 2."))
            return
        print()

        n_added = run_artist_missing_albums(artist_name, owned_bare_titles, args, token,
                                    seed_artist_id=seed_id,
                                    seen_album_ids=seen_album_ids,
                                    prefetched_catalog=prefetched_catalog)
        if n_added:
            log.info(fmt(C.GREEN, f"\n  ✓  Added {n_added} new album(s) for {artist_name}.\n"))
    finally:
        # Restore --consolidate so the menu loop sees the original CLI value.
        args.consolidate = saved_consolidate
