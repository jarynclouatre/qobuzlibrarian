"""Album consolidation — find and remove duplicate sibling folders.

"""
from pathlib import Path

from qobuz_librarian import config
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


def find_sibling_album_dirs(album, primary_dir):
    """Find other album dirs by the same artist with similar bare names.

    Threshold: CONSOLIDATE_THRESH (0.70). 'Revolver' ↔ 'Revolver
    (2009 Remaster)' = 1.0 after stripping; 'Revolver' ↔ 'Greatest Hits'
    = ~0.15. Returns list of (Path, score) sorted desc.
    """
    if primary_dir is None or not primary_dir.parent.exists():
        return []
    artist_dir = primary_dir.parent
    primary_bare = strip_album_decorations(primary_dir.name)

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
        d_bare = strip_album_decorations(d.name)
        s1 = similarity(d_bare, primary_bare)
        s2 = similarity(d_bare, api_bare) if api_bare else 0.0
        score = max(s1, s2)
        if score >= config.CONSOLIDATE_THRESH:
            siblings.append((d, score))
    return sorted(siblings, key=lambda x: -x[1])


def match_sibling_track(sibling_track, primary_tracks):
    """Match sibling → primary by ISRC > MBID > (disc, normalized title)."""
    s_isrc = (sibling_track.get("isrc") or "").upper()
    s_mbid = (sibling_track.get("mb_trackid") or "").lower()
    s_title_norm = normalize(sibling_track.get("title", ""))
    s_disc = sibling_track.get("discnumber", 1) or 1

    if s_isrc:
        for pt in primary_tracks:
            if (pt.get("isrc") or "").upper() == s_isrc:
                return pt
    if s_mbid:
        for pt in primary_tracks:
            if (pt.get("mb_trackid") or "").lower() == s_mbid:
                return pt
    if s_title_norm:
        for pt in primary_tracks:
            if (normalize(pt.get("title", "")) == s_title_norm
                    and (pt.get("discnumber", 1) or 1) == s_disc):
                return pt
    return None


def consolidation_summary(siblings, primary_tracks):
    """For each sibling: classify each track as overlap or unique."""
    summaries = []
    for sib_dir, score in siblings:
        sib_tracks = read_album_dir(sib_dir)
        overlap, unique = [], []
        for st in sib_tracks:
            match = match_sibling_track(st, primary_tracks)
            if match:
                overlap.append((st, match))
            else:
                unique.append(st)
        summaries.append({
            "dir": sib_dir, "score": score, "all_tracks": sib_tracks,
            "overlap": overlap, "unique": unique,
        })
    return summaries


def execute_consolidation(summary):
    n_deleted = n_failed = 0
    for st, _ in summary["overlap"]:
        path = Path(st.get("path", ""))
        if not path.exists():
            n_failed += 1
            continue
        try:
            path.unlink()
            n_deleted += 1
            vlog(f"deleted {path}")
        except OSError as e:
            n_failed += 1
            log.info(fmt(C.RED, f"      ✗  failed to delete {path.name}: {e}."))
    return n_deleted, n_failed


def consolidate_albums(album, args):
    """Top-level consolidation flow. Always interactive — --yes does NOT silence it."""
    from qobuz_librarian.library.catalog import find_album_dir_filesystem
    from qobuz_librarian.library.scanner import read_album_dir as _rad

    section("Consolidate similar album folders")

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
        warning = qc["losing_hires"] > 0

        print()
        log.info(fmt(C.BOLD + C.WHITE, f"  Sibling: {truncate(sib_dir.name, 55)}"))
        log.info(fmt(C.GRAY,
            f"    {n_over} track(s) overlap • {len(s['unique'])} bonus track(s) will remain"))
        if warning:
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
                        f"    Type 'DELETE' to confirm losing hi-res on {qc['losing_hires']} track(s):"))
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
                n_del, n_fail = execute_consolidation(s)
                if n_del:
                    log.info(fmt(C.GREEN, f"    ✓  Deleted {plural(n_del, 'track')}."))
                if n_fail:
                    log.info(fmt(C.RED, f"    ✗  Failed to delete {plural(n_fail, 'track')}."))
                n_actioned += n_del
                # Offer to remove empty dir if no bonus tracks remain
                if not s["unique"] and n_del:
                    if maybe_remove_empty_dir(sib_dir):
                        log.info(fmt(C.GREEN, f"    ✓  Removed empty folder: {sib_dir.name}."))
                break
            log.info(fmt(C.GRAY, "    Enter d / s / k / q."))

    return n_actioned
