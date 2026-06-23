"""Album consolidation — find and remove duplicate sibling folders."""
import re
from pathlib import Path

from qobuz_librarian import config
from qobuz_librarian.integrations.beets import forget_beets_entries
from qobuz_librarian.library.catalog import maybe_remove_empty_dir
from qobuz_librarian.library.scanner import read_album_dir
from qobuz_librarian.library.tags import normalize, similarity, strip_album_decorations
from qobuz_librarian.quality.decision import quality_change_summary
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import (
    confirm,
    print_consolidation_overview,
    print_per_track_consolidation,
)

_YEAR_RE = re.compile(r"(?<!\d)(19\d\d|20\d\d)(?!\d)")


def _years_in(name: str) -> set:
    """The set of plausible release years embedded in a folder name."""
    return set(_YEAR_RE.findall(name or ""))


def find_sibling_album_dirs(album, primary_dir):
    """Find other album dirs by the same artist with similar bare names.

    Threshold: CONSOLIDATE_THRESH (0.70). 'Revolver' ↔ 'Revolver
    (2009 Remaster)' = 1.0 after stripping; 'Revolver' ↔ 'Greatest Hits'
    = ~0.15. Returns list of (Path, score) sorted desc.

    Folders that BOTH carry a year and whose years don't overlap are never
    grouped: 'Live at Wembley 1990' / '… 1992', 'Live 1971' / '1972', or an
    original vs a differently-dated reissue are distinct works that happen to
    share a name, and consolidation deletes "duplicate" tracks — grouping them
    would feed a distinct recording to the deleter. A one-sided year ('Album'
    vs 'Album (2020)') still groups, since that's the same release re-tagged.
    """
    if primary_dir is None or not primary_dir.parent.exists():
        return []
    artist_dir = primary_dir.parent
    primary_bare = strip_album_decorations(primary_dir.name)
    primary_years = _years_in(primary_dir.name) or _years_in(album.get("title") or "")

    api_title = album.get("title") or ""
    api_bare = strip_album_decorations(api_title)

    siblings = []
    try:
        subdirs = [d for d in artist_dir.iterdir() if d.is_dir()]
    except OSError:
        return []
    for d in subdirs:
        try:
            if d.resolve() == primary_dir.resolve():
                continue
        except OSError:
            continue
        d_years = _years_in(d.name)
        if primary_years and d_years and primary_years.isdisjoint(d_years):
            continue
        d_bare = strip_album_decorations(d.name)
        s1 = similarity(d_bare, primary_bare)
        s2 = similarity(d_bare, api_bare) if api_bare else 0.0
        score = max(s1, s2)
        if score >= config.CONSOLIDATE_THRESH:
            siblings.append((d, score))
    return sorted(siblings, key=lambda x: -x[1])


def _same_recording_signal(a, b):
    """Positive 'same recording' evidence for two tracks that share a title but
    have no ISRC/MBID to confirm identity: their durations must agree within ~2s.

    When either side has no readable duration there is NO real evidence, so this
    returns False and the caller keeps both copies. A title (and even a track
    slot) can coincide between two genuinely different recordings — file size is
    too weak a tiebreak to risk an irreversible delete on, so it is not used."""
    la, lb = a.get("length") or 0.0, b.get("length") or 0.0
    return la > 0 and lb > 0 and abs(la - lb) <= 2.0


def match_sibling_track(sibling_track, primary_tracks):
    """Match sibling → primary by ISRC > MBID > (disc, title + same recording)."""
    s_isrc = (sibling_track.get("isrc") or "").upper()
    s_mbid = (sibling_track.get("mb_trackid") or "").lower()
    s_title_norm = normalize(sibling_track.get("title", ""))
    s_disc = sibling_track.get("discnumber", 1) or 1
    s_track = sibling_track.get("tracknumber") or 0

    if s_isrc:
        for pt in primary_tracks:
            if (pt.get("isrc") or "").upper() == s_isrc:
                return pt
        # The sibling has an ISRC but no primary track matches it. If any
        # primary also carries an ISRC, that's positive evidence these are
        # different recordings (e.g. 'Fearless' vs 'Fearless (Taylor's
        # Version)'), so don't let the title+position fallback below offer the
        # re-recorded track for deletion as a duplicate.
        if any((pt.get("isrc") or "").upper() for pt in primary_tracks):
            return None
    if s_mbid:
        for pt in primary_tracks:
            if (pt.get("mb_trackid") or "").lower() == s_mbid:
                return pt
    if s_title_norm:
        # Title + disc + position is NOT recording identity without an ISRC/MBID:
        # a live setlist replayed on another date (or any two distinct
        # same-titled recordings sharing a slot) matches all three. So the title
        # fallback additionally requires the durations to agree before declaring
        # an overlap the deleter will unlink — and where both sides carry a track
        # number, those must match too. (find_sibling_album_dirs separately
        # refuses to group folders that differ by year — the other half of this.)
        for pt in primary_tracks:
            if (normalize(pt.get("title", "")) != s_title_norm
                    or (pt.get("discnumber", 1) or 1) != s_disc):
                continue
            p_track = pt.get("tracknumber") or 0
            if s_track and p_track and p_track != s_track:
                continue
            if _same_recording_signal(sibling_track, pt):
                return pt
    return None


def consolidation_summary(siblings, primary_tracks):
    """For each sibling: classify each track as overlap or unique."""
    summaries = []
    for sib_dir, score in siblings:
        sib_tracks = read_album_dir(sib_dir)
        overlap, unique = [], []
        # Each primary track is one recording: at most one sibling track may
        # claim it as a duplicate. Two distinct sibling files fuzzy-matching the
        # same primary (two 'Intro's, suite parts) would otherwise BOTH be
        # deleted, destroying the one that has no real copy in primary.
        claimed = set()
        for st in sib_tracks:
            match = match_sibling_track(st, primary_tracks)
            if match is not None and id(match) not in claimed:
                claimed.add(id(match))
                overlap.append((st, match))
            else:
                unique.append(st)
        summaries.append({
            "dir": sib_dir, "score": score, "all_tracks": sib_tracks,
            "overlap": overlap, "unique": unique,
        })
    return summaries


def execute_consolidation(summary):
    """Move the overlapping sibling tracks to a recoverable backup instead of
    hard-deleting them, then return (removed_paths, n_failed) so the caller can
    drop exactly those files from the beets DB.

    Consolidation is the one destructive mode whose duplicate match can rest on a
    title+disc+(track)+duration-within-2s heuristic when neither side carries an
    ISRC/MBID, so a mistaken match would otherwise unlink a genuinely different
    recording with no way back. Routing removals through the gap-fill backup dir
    lets the retention sweep recover them, matching every other destructive
    mode's keep-a-backup stance."""
    from qobuz_librarian.library.backup import backup_gap_fill_files

    to_remove, n_failed = [], 0
    for st, _ in summary["overlap"]:
        raw = (st.get("path") or "").strip()
        if not raw:
            # Malformed overlap entry with no path — nothing to remove. Skip it
            # rather than letting Path("") resolve to the current dir.
            vlog("consolidation: skipping overlap entry with no path")
            continue
        path = Path(raw)
        if not path.exists():
            n_failed += 1
            continue
        to_remove.append(path)

    if not to_remove:
        return [], n_failed

    # Move (don't unlink): backup_gap_fill_files renames/copies each file into a
    # timestamped backup dir and only then removes the source, leaving anything
    # it couldn't move on disk. A file gone from its original path landed in the
    # backup; one still present failed to move and is counted as a failure.
    backup_gap_fill_files(to_remove, Path(summary["dir"]))
    removed = []
    for path in to_remove:
        if path.exists():
            n_failed += 1
            log.info(fmt(C.RED,
                f"      ✗  couldn't move {path.name} to backup; left in place."))
        else:
            removed.append(path)
            vlog(f"consolidation: moved {path} to backup")
    return removed, n_failed


def consolidate_albums(album, args):
    """Top-level consolidation flow. Always interactive — --yes does NOT silence it."""
    from qobuz_librarian.library.catalog import find_album_dir_filesystem
    from qobuz_librarian.library.scanner import read_album_dir as _rad

    section("Consolidate similar album folders")
    # Consolidation deletes overlapping sibling tracks, so it must never run
    # under --dry-run — including on the "already complete" album path, which
    # reaches here before process_album's own dry-run stop.
    if getattr(args, "dry_run", False):
        log.info(fmt(C.GRAY,
            "  --dry-run: skipping consolidation (it would delete overlapping tracks)."))
        return 0

    primary_dir = find_album_dir_filesystem(album)
    if primary_dir is None:
        log.info(fmt(C.YELLOW, "  ⚠  Couldn't locate primary album folder after import."))
        log.info(fmt(C.GRAY,   "     Skipping consolidation."))
        return 0
    log.info(fmt(C.GRAY, f"  Primary: {primary_dir}"))

    primary_tracks = _rad(primary_dir)
    if not primary_tracks:
        log.info(fmt(C.YELLOW, "  ⚠  No tracks read from primary folder. Skipping consolidation."))
        return 0

    siblings = find_sibling_album_dirs(album, primary_dir)
    if not siblings:
        log.info(fmt(C.GREEN, "  ✓  No sibling album folders found. Nothing to consolidate."))
        return 0

    summaries = consolidation_summary(siblings, primary_tracks)
    print_consolidation_overview(summaries)

    print()
    log.info(fmt(C.BOLD + C.CYAN, "  Per-sibling actions:"))
    n_actioned = 0

    for s in summaries:
        sib_dir = s["dir"]
        n_over = len(s["overlap"])
        if not n_over:
            continue

        qc = quality_change_summary(s["overlap"])
        risky = qc["losing_hires"] + qc["unknown"]
        warning = risky > 0

        print()
        log.info(fmt(C.BOLD + C.WHITE, f"  Sibling: {truncate(sib_dir.name, 55)}"))
        log.info(fmt(C.GRAY,
            f"    {n_over} track(s) overlap • {len(s['unique'])} bonus track(s) will remain"))
        if warning:
            if qc["unknown"]:
                log.info(fmt(C.RED + C.BOLD,
                    f"    ⚠  {risky} track(s) here could lose quality "
                    f"({qc['losing_hires']} higher than primary, "
                    f"{qc['unknown']} unreadable)!"))
            else:
                log.info(fmt(C.RED + C.BOLD,
                    f"    ⚠  {qc['losing_hires']} track(s) here are HIGHER quality than primary!"))
            print_per_track_consolidation(s)

        log.info(fmt(C.WHITE, "    [d] delete overlapping tracks (default)"))
        log.info(fmt(C.WHITE, "    [s] show per-track details"))
        log.info(fmt(C.WHITE, "    [k] keep this sibling untouched"))
        log.info(fmt(C.WHITE, "    [q] stop consolidation entirely"))

        while True:
            try:
                choice = input(fmt(C.CYAN, "    Choice [d]: ")).strip().lower() or "d"
            except EOFError:
                choice = "k"
            if choice == "s":
                print_per_track_consolidation(s)
                continue
            if choice == "k":
                log.info(fmt(C.GRAY, "    Skipped."))
                break
            if choice == "q":
                log.info(fmt(C.GRAY, "    Stopping consolidation."))
                return n_actioned
            if choice == "d":
                if warning:
                    log.info(fmt(C.YELLOW + C.BOLD,
                        f"    Type 'DELETE' to confirm a possible quality loss "
                        f"on {risky} track(s):"))
                    try:
                        typed = input(fmt(C.CYAN, "    > ")).strip()
                    except EOFError:
                        typed = ""
                    if typed != "DELETE":
                        log.info(fmt(C.GRAY, "    Confirmation not given. Skipped."))
                        break
                else:
                    if not confirm(f"    Delete {n_over} track(s) from this sibling?",
                                   default_yes=True, auto_yes=False):
                        log.info(fmt(C.GRAY, "    Skipped."))
                        break
                deleted, n_fail = execute_consolidation(s)
                n_del = len(deleted)
                if n_del:
                    log.info(fmt(C.GREEN, f"    ✓  Deleted {plural(n_del, 'track')}."))
                if n_fail:
                    log.info(fmt(C.RED, f"    ✗  Failed to delete {plural(n_fail, 'track')}."))
                n_actioned += n_del
                # Drop the deleted tracks from the beets library too, so a
                # hand-run `beet` doesn't keep listing files that are gone.
                if deleted and forget_beets_entries(deleted):
                    log.info(fmt(C.GRAY, "    ⤷  Updated the beets library."))
                # Offer to remove empty dir if no bonus tracks remain
                if not s["unique"] and n_del:
                    if maybe_remove_empty_dir(sib_dir):
                        log.info(fmt(C.GREEN, f"    ✓  Removed empty folder: {sib_dir.name}."))
                break
            log.info(fmt(C.GRAY, "    Enter d / s / k / q."))

    return n_actioned
