"""Quality comparison helpers and upgrade detection."""
import json
import time
from datetime import datetime, timedelta, timezone

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzError
from qobuz_librarian.api.search import get_artist_albums, search_artists
from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.catalog import (
    album_quality_label,
    compute_missing,
    find_existing_tracks,
    find_extras_in_existing,
    is_lossless_album,
)
from qobuz_librarian.library.scanner import clear_scan_caches, list_artist_album_dirs
from qobuz_librarian.library.tags import similarity, strip_album_decorations
from qobuz_librarian.quality.tiers import downsample_target_rate, streamrip_quality_cap
from qobuz_librarian.ui_cli.logging import vlog


def album_max_quality(qobuz_album):
    """Return the (bit_depth, sample_rate_hz) the pipeline will actually put
    on disk for a Qobuz album — capped to the streamrip quality tier, then
    through the downsample hook if it's enabled.

    Qobuz reports sample rate in kHz (e.g. 96.0); values >= 1000 are treated
    as already-Hz so an API change to Hz can't make every album read as
    'lower than Qobuz' and trigger spurious upgrades.

    Comparing against the *post-downsample* rate matters: with downsampling
    on, a 24/192 album lands as 24/48, so capping only to the rip tier would
    flag every already-downsampled album as below target and re-rip it on
    every scan, forever.
    """
    bd = qobuz_album.get("maximum_bit_depth") or 0
    sr = qobuz_album.get("maximum_sampling_rate") or 0
    sr_hz = int(round(sr)) if sr >= 1000 else int(round(sr * 1000))
    cap_bd, cap_sr = streamrip_quality_cap()
    bd = min(bd, cap_bd) if bd else 0
    sr_hz = min(sr_hz, cap_sr) if sr_hz else 0
    if sr_hz and cfg.DOWNSAMPLE_HIRES_ENABLED and HAVE_DOWNSAMPLE:
        sr_hz = downsample_target_rate(sr_hz)
    return (bd, sr_hz)


def existing_track_quality(track):
    """Return (bit_depth, sample_rate_hz) for an existing track dict.
    Mutagen's FLAC.info gives sample_rate in Hz directly; bits_per_sample is
    bits (16, 24)."""
    return (track.get("bits") or 0, track.get("sample_rate") or 0)


def compare_album_quality(existing_tracks, qobuz_album):
    """Classify per-track quality of existing tracks vs the Qobuz album.

    Returns dict with classification (str) and counts. Classifications:
      'no_existing' : existing list is empty
      'unknown'     : couldn't read quality from any existing track
      'all_lower'   : every readable track is below Qobuz quality
      'mixed_below' : some below, some at; none above
      'all_equal'   : every readable track matches Qobuz quality
      'all_higher'  : every readable track is above Qobuz quality
      'mixed_above' : some above, some at; none below
      'mixed_both'  : some below AND some above (rare; treat conservatively)

    Compares as tuples so (24, 96000) > (16, 44100) and (16, 96000) >
    (16, 44100) etc — both bit depth and sample rate matter.
    """
    qbits, qrate = album_max_quality(qobuz_album)
    n_below = n_at = n_above = n_unknown = 0

    for t in existing_tracks:
        ebits, erate = existing_track_quality(t)
        if not ebits or not erate:
            n_unknown += 1
            continue
        if (ebits, erate) < (qbits, qrate):
            n_below += 1
        elif (ebits, erate) > (qbits, qrate):
            n_above += 1
        else:
            n_at += 1

    n_known = n_below + n_at + n_above
    if not existing_tracks:
        cls = "no_existing"
    elif n_known == 0:
        cls = "unknown"
    elif n_below > 0 and n_above > 0:
        cls = "mixed_both"
    elif n_below > 0:
        cls = "all_lower" if n_at == 0 else "mixed_below"
    elif n_above > 0:
        cls = "all_higher" if n_at == 0 else "mixed_above"
    else:
        cls = "all_equal"

    return {
        "classification": cls,
        "qobuz_quality": (qbits, qrate),
        "n_below": n_below,
        "n_at": n_at,
        "n_above": n_above,
        "n_unknown": n_unknown,
    }


def _track_quality_cmp(t1, t2):
    """Compare audio quality of two track dicts.
    Returns 1 if t1 > t2, -1 if t1 < t2, 0 if equal.
    Keyed on (bit_depth, sample_rate) matching --prefer-hires sort order.
    """
    q1 = (t1.get("bits") or 0, t1.get("sample_rate") or 0)
    q2 = (t2.get("bits") or 0, t2.get("sample_rate") or 0)
    return (q1 > q2) - (q1 < q2)


def quality_change_summary(overlap):
    """Count tracks that would be a downgrade if deleted."""
    losing_hires = same = upgrading = 0
    for st, pt in overlap:
        cmp = _track_quality_cmp(st, pt)
        if cmp > 0:
            losing_hires += 1
        elif cmp < 0:
            upgrading += 1
        else:
            same += 1
    return {"losing_hires": losing_hires, "same": same, "upgrading": upgrading}


# ── Upgrade-cap persistence ───────────────────────────────────────────────────

def load_capped():
    if not cfg.CAPPED_FILE.exists():
        return {}
    try:
        with open(cfg.CAPPED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # A malformed file that parses as a list/string would otherwise reach
        # is_album_capped's `.get` and crash the upgrade scan.
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _capped_ts(entry):
    # Parse an entry's "ts" into a tz-aware datetime, or None if missing/bad.
    try:
        ts = datetime.fromisoformat((entry or {}).get("ts", ""))
    except (ValueError, TypeError, AttributeError):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _capped_is_fresh(ts):
    # Single staleness boundary shared by the read and write paths, so an
    # entry can't read as still-capped but get pruned on write (or the
    # reverse) right at the edge of the retention window.
    if ts is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.CAPPED_RETENTION_DAYS)
    return ts >= cutoff


def save_capped(data):
    # Prune stale entries before writing. is_album_capped treats them as "not
    # capped" but never removes them, so the file would otherwise accumulate
    # dead weight on every mark_album_capped call. Cheap to clean here.
    fresh = {k: v for k, v in (data or {}).items() if _capped_is_fresh(_capped_ts(v))}
    try:
        tmp = cfg.CAPPED_FILE.with_suffix(cfg.CAPPED_FILE.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fresh, f, indent=2, ensure_ascii=False)
        tmp.replace(cfg.CAPPED_FILE)
    except OSError:
        pass


def is_album_capped(album_id, capped):
    if not album_id:
        return False
    return _capped_is_fresh(_capped_ts(capped.get(str(album_id))))


def mark_album_capped(album_id, qobuz_album, post_qual):
    if not album_id:
        return
    capped = load_capped()
    capped[str(album_id)] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "title": qobuz_album.get("title"),
        "artist": (qobuz_album.get("artist") or {}).get("name"),
        "qobuz_advertised": album_quality_label(qobuz_album),
        "actual_n_at_target": post_qual.get("n_at", 0),
        "actual_n_below": post_qual.get("n_below", 0),
        "actual_n_above": post_qual.get("n_above", 0),
    }
    save_capped(capped)


# ── Upgrade-candidate scan ────────────────────────────────────────────────────

def scan_artist_for_upgrades(artist_name, artist_dir, token, args, capped=None):
    """Silently scan all album dirs under artist_dir for quality upgrade candidates.

    Uses the same catalog pre-fetch + local matching strategy as gap-fill to
    minimise API round-trips. Falls back to per-folder search on catalog miss.

    Returns a list of dicts (one per upgradeable album):
      {qobuz_album, album_dir, n_below,
       existing_quality_label, target_quality_label}

    Only includes albums where:
      • Qobuz offers strictly higher quality (all_lower / mixed_below)
      • Existing tracks are present on disk
      • No bonus tracks would be lost (extras block wipe-and-replace)
      • --no-upgrade is not set
    """
    from qobuz_librarian.library.catalog import find_expanded_edition, find_qobuz_album_for_dir

    clear_scan_caches()
    album_dirs = list_artist_album_dirs(artist_dir)
    if not album_dirs:
        return []

    # Pre-fetch artist catalog once so per-folder matching stays local.
    # Failure here is non-fatal; find_qobuz_album_for_dir falls back to search.
    catalog = []
    try:
        artists = search_artists(artist_name, token, limit=cfg.ARTIST_LOOKUP_LIMIT)
        if artists:
            best_a = max(artists, key=lambda a: similarity(a.get("name", ""), artist_name))
            if similarity(best_a.get("name", ""), artist_name) >= cfg.ARTIST_NAME_THRESH:
                artist_id = best_a.get("id")
                catalog, _ = get_artist_albums(artist_id, token, limit=cfg.ARTIST_CATALOG_LIMIT)
    except QobuzError:
        pass

    # Grabbed singles sit out the upgrade walk unless the user opted in.
    single_store = None if cfg.UPGRADE_SINGLES_ENABLED else hidden_mod.load()
    candidates = []
    for album_dir in album_dirs:
        try:
            qobuz_album = find_qobuz_album_for_dir(
                album_dir, artist_name, token,
                prefer_hires=args.prefer_hires,
                catalog=catalog,
                target_dir=album_dir,
            )
        except AuthLost:
            raise
        except QobuzError:
            vlog(f"    QobuzError matching {album_dir.name}; skipping")
            continue

        if qobuz_album is None or not is_lossless_album(qobuz_album):
            continue

        if single_store is not None and hidden_mod.is_single(
                artist_name, qobuz_album.get("title"), single_store):
            continue

        # Skip albums Qobuz can't actually deliver at target.
        if capped and is_album_capped(qobuz_album.get("id"), capped):
            vlog(f"    {album_dir.name}: capped (partial hi-res previously)")
            continue

        qobuz_tracks = (qobuz_album.get("tracks") or {}).get("items") or []
        if not qobuz_tracks:
            continue

        existing, _ = find_existing_tracks(qobuz_album)
        if not existing:
            continue  # nothing on disk — not an upgrade scenario

        # False-match guard: if no tracks overlap the Qobuz result is wrong.
        _, present = compute_missing(qobuz_tracks, existing)
        if not present:
            continue

        if getattr(args, "no_upgrade", False):
            continue

        qual = compare_album_quality(existing, qobuz_album)
        if qual["classification"] not in ("all_lower", "mixed_below"):
            continue

        # An unreadable track can't be verified as an upgrade, and the
        # wipe-replace in process_album refuses the whole album when any
        # exist. Don't surface a candidate it would only no-op on.
        if qual["n_unknown"]:
            continue

        # Bonus tracks normally block wipe-and-replace. Before giving up,
        # try to find an alternate Qobuz edition that covers everything
        # on disk. Auto-promote a perfect match (no new extras, still an
        # upgrade) silently — upgrade walk has no per-album picker by
        # design. Critical for libraries built from messy partial-edition
        # rips, where one stray track from a different edition currently
        # blocks the entire upgrade.
        extras = find_extras_in_existing(qobuz_tracks, existing)
        if extras:
            cands = find_expanded_edition(qobuz_album, album_dir,
                                          existing, token, args)
            swapped = False
            for full, new_extras in cands:
                if new_extras:
                    continue
                # A swapped-to edition has its own id, so its cap marker is
                # separate from the one we checked above — honour it here or
                # a capped expanded edition re-flags on every scan.
                if capped and is_album_capped(full.get("id"), capped):
                    continue
                new_qual = compare_album_quality(existing, full)
                if new_qual["classification"] in ("all_lower", "mixed_below"):
                    qobuz_album = full
                    qobuz_tracks = (full.get("tracks") or {}).get("items") or []
                    qual = new_qual
                    swapped = True
                    break
            if not swapped:
                continue

        # Build human-readable quality labels for the upgrade summary.
        qbits, qrate = qual["qobuz_quality"]
        target_label = (f"{qbits}-bit/{qrate / 1000:g}kHz"
                        if qbits and qrate else "Qobuz quality")

        _qcounts: dict = {}
        for t in existing:
            bb, rr = existing_track_quality(t)
            if bb and rr:
                _qcounts[(bb, rr)] = _qcounts.get((bb, rr), 0) + 1
        if _qcounts:
            eb, er = max(_qcounts, key=_qcounts.get)
            existing_label = f"{eb}-bit/{er / 1000:g}kHz"
            if len(_qcounts) > 1:
                existing_label = "~" + existing_label
        else:
            existing_label = "lower quality"

        # Capture confidence signals for --auto-safe.
        # 'extras' here is the var from the bonus-track guard above —
        # truthy means an edition swap was used to cover on-disk tracks.
        _folder_bare = strip_album_decorations(album_dir.name)
        _title_bare  = strip_album_decorations(qobuz_album.get("title", "") or "")
        _title_sim   = similarity(_folder_bare, _title_bare)
        _needed_swap = bool(extras)

        candidates.append({
            "qobuz_album":            qobuz_album,
            "album_dir":              album_dir,
            "n_present":              len(existing),
            "n_total":                len(qobuz_tracks),
            "n_below":                qual["n_below"],
            "existing_quality_label": existing_label,
            "target_quality_label":   target_label,
            "_needed_edition_swap":   _needed_swap,
            "_title_similarity":      _title_sim,
        })
        time.sleep(cfg.ARTIST_API_DELAY)

    return candidates
