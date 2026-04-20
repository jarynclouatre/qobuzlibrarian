"""Album/artist catalog matching and track-presence detection.

Behaviour you should not change without understanding the consequence:

- find_album_dir_filesystem: exact-path fast path first, then fuzzy scan.
  Artist-variant expansion covers "The X" / "X" / comma-prefix forms.
  Multi-artist folder detection (_MULTI_ARTIST_SEPS) expands search dirs.
- compute_missing: 3-layer matching chain in order —
    1. ISRC — authoritative recording identity
    2. (disc, normalized title)
    3. edition-stripped variants on BOTH sides
  A track is "present" if ANY layer matches. Order matters: ISRC is
  authoritative; title-match is a fallback. Reordering causes false
  re-downloads of already-owned tracks. compute_missing and
  find_extras_in_existing share one matcher so the two never drift.
- album_year(): prefers release_date_original; falls back to released_at
  parsed in UTC. Local-timezone parsing flipped years for late-night-UTC
  releases.
- predicted_album_paths uses a list, not a set, to preserve deterministic
  candidate order across runs (set hash iteration is non-deterministic).
"""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from qobuz_librarian import config
from qobuz_librarian.api.auth import QobuzError
from qobuz_librarian.api.search import get_album, search_albums
from qobuz_librarian.library.scanner import (
    _list_artist_subdirs_cached,
    iter_tree_no_symlinks,
    read_album_dir,
)
from qobuz_librarian.library.tags import (
    beets_sanitize,
    normalize,
    similarity,
    strip_album_decorations,
    strip_edition_suffix,
    strip_year_decoration,
)
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log, vlog

# ── Multi-artist folder detection ─────────────────────────────────────────────
# Beets writes ALBUMARTIST = "X, Y" for multi-artist releases.
# Qobuz only returns the first artist. These separators widen the folder
# search so existing multi-artist folders are found without re-downloading.
_MULTI_ARTIST_SEPS = (
    ", ", " & ", " and ", " feat. ", " feat ",
    " ft. ", " ft ", " with ", " x ", " vs. ", " vs ",
)
# Migration uses comma-space ONLY — other separators appear in real band
# names ('Bob Marley & The Wailers') and auto-migrating those would
# silently fold entire artists' catalogs into a lead-member folder.
_MIGRATION_SEPS = (", ",)
_PRIMARY_ARTIST_SEPS = (", ", " & ", " and ", " feat. ", " feat ", " ft. ", " ft ")


def _primary_artist_of(qartist):
    """Return the primary (first) name from a multi-artist string.

    Qobuz returns the same album with different artist-string formats
    depending on which edition is queried: "Jay Z and Kanye West"
    (album-level) vs. "Jay Z, Kanye West" (track-level). The migration
    check needs a canonical primary so it matches the on-disk folder
    regardless of which form Qobuz happened to return.
    """
    if not qartist:
        return qartist
    s = qartist.strip()
    for sep in _PRIMARY_ARTIST_SEPS:
        if sep in s:
            return s.split(sep, 1)[0].strip()
    return s


def _has_separator_match(folder_name, qartist, seps):
    if not folder_name or not qartist:
        return False
    qa = qartist.strip()
    fl = folder_name.strip()
    if len(fl) <= len(qa):
        return False
    if fl[:len(qa)].lower() != qa.lower():
        return False
    rest = fl[len(qa):]
    return any(rest.startswith(sep) for sep in seps)


def _is_multi_artist_subset(folder_name, qartist):
    """True iff folder_name is 'qartist<sep><other>' for a detection sep."""
    return _has_separator_match(folder_name, qartist, _MULTI_ARTIST_SEPS)


def _is_migration_candidate(folder_name, qartist):
    """True iff the folder is safe to migrate (comma-space form only)."""
    return _has_separator_match(folder_name, qartist, _MIGRATION_SEPS)


def _find_multi_artist_dirs(qartist):
    """Return MUSIC_ROOT children whose name is 'qartist<sep><other>'.
    Uses the scan cache so artist-mode runs don't re-iterdir."""
    if not qartist or not config.MUSIC_ROOT.exists():
        return []
    matches = []
    for d in _list_artist_subdirs_cached(config.MUSIC_ROOT):
        if _is_multi_artist_subset(d.name, qartist):
            matches.append(d)
    return matches


# ── Path utilities ────────────────────────────────────────────────────────────
def _paths_equal(a: Path, b: Path) -> bool:
    """Best-effort path equality. samefile() is authoritative but raises when a
    path doesn't exist; fall back to comparing resolved (symlink-collapsed)
    forms, then to a plain string normalization."""
    try:
        return a.samefile(b)
    except OSError:
        pass
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return os.path.normpath(str(a)) == os.path.normpath(str(b))


_DECORATION_YEAR_RE = re.compile(
    r"[(\[](\d{4})[)\]]\s*$"      # trailing '(2010)' / '[2010]'
    r"|^\[(\d{4})\]\s+"          # leading '[2010] Title'
    r"|^(\d{4})\s*[-–—]\s+"      # leading '2010 - Title'
)


def _decoration_year(name):
    """The 4-digit year a folder name uses as a decoration ('' if none).

    A bare year that is itself the title ('1984', '2112') is not a decoration;
    a year in a clear slot — trailing '(2010)'/'[2010]', or a leading '[2010] '
    or '2010 - ' prefix — is. Mirrors tags.py's year-stripping rules."""
    m = _DECORATION_YEAR_RE.search(name or "")
    return next((g for g in m.groups() if g), "") if m else ""


def _is_split_album_merge(album_dir, post_dir, qartist):
    """True when album_dir and post_dir are the SAME album split across two
    folders, safe to consolidate into post_dir (where beets just filed the new
    tracks). Two shapes:

      - multi-artist: existing tracks under 'Primary, Other/Album', new ones
        under 'Primary/Album'.
      - year decoration: a hand-named/migrated folder lacks the '($year)' beets
        writes ('Black Sands' vs 'Black Sands (2010)').

    Both require the two names to be the same album: equal once the year tag is
    stripped (so an edition/live folder isn't fused with the studio album), and
    not pinning two DIFFERENT years (different years are different releases — a
    reissue, an annual live album — never one split). Resolution can fuzzy-match
    a similarly-titled but DIFFERENT album into album_dir, so these string
    guards are what stop an unrelated album from being consumed.
    """
    if album_dir is None or post_dir is None:
        return False
    try:
        if not album_dir.exists() or not post_dir.exists():
            return False
    except OSError:
        return False
    if _paths_equal(post_dir, album_dir) or not qartist:
        return False
    a = normalize(strip_year_decoration(album_dir.name))
    if not a or a != normalize(strip_year_decoration(post_dir.name)):
        return False
    ay, py = _decoration_year(album_dir.name), _decoration_year(post_dir.name)
    if ay and py and ay != py:
        return False
    if (_is_multi_artist_subset(album_dir.parent.name, qartist)
            and not _is_multi_artist_subset(post_dir.parent.name, qartist)):
        return True
    return bool(_paths_equal(album_dir.parent, post_dir.parent))


# ── Album dir resolution ──────────────────────────────────────────────────────
def predicted_album_paths(qobuz_album):
    """Build an ordered list of candidate paths for a Qobuz album.

    List (not set): set iteration order is hash-based, causing non-deterministic
    candidate resolution across runs.
    """
    artist_raw = (qobuz_album.get("artist") or {}).get("name") or ""
    artist = beets_sanitize(artist_raw)
    title  = beets_sanitize(qobuz_album.get("title") or "")

    candidates = []
    if not artist or not title:
        return candidates

    artist_dirs = [config.MUSIC_ROOT / artist]
    artist_dirs.extend(_find_multi_artist_dirs(artist_raw))
    # Qobuz returns "Jay Z and Kanye West" on some editions while the folder
    # is "Jay Z, Kanye West"; searching multi-artist dirs under the primary
    # name finds it, so the migration that follows isn't starved of a source dir.
    primary_raw = _primary_artist_of(artist_raw)
    if primary_raw and primary_raw != artist_raw:
        for d in _find_multi_artist_dirs(primary_raw):
            if d not in artist_dirs:
                artist_dirs.append(d)

    years = []
    yr = album_year(qobuz_album)
    if yr:
        years.append(yr)

    bare_titles = [title]
    stripped = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
    if stripped and stripped != title:
        bare_titles.append(stripped)

    for ad in artist_dirs:
        for bt in bare_titles:
            for year in years:
                # Common beets path-template forms: trailing-paren, leading
                # [year], leading bare year. Match all three so a user who
                # switched beets templates doesn't get the whole library
                # listed as "missing".
                candidates.append(ad / f"{bt} ({year})")
                candidates.append(ad / f"[{year}] {bt}")
                candidates.append(ad / f"{year} - {bt}")
            candidates.append(ad / bt)

    if artist_raw.lower().strip() in (
            "various artists", "various", "va", "compilations", "soundtrack"):
        for bt in bare_titles:
            for year in years:
                candidates.append(
                    config.MUSIC_ROOT / "Various Artists" / f"{bt} ({year})")
                candidates.append(
                    config.MUSIC_ROOT / "Various Artists" / f"[{year}] {bt}")
                candidates.append(
                    config.MUSIC_ROOT / "Various Artists" / f"{year} - {bt}")
            candidates.append(config.MUSIC_ROOT / "Various Artists" / bt)

    return candidates


def find_album_dir_filesystem(qobuz_album):
    """Resolve a Qobuz album dict to its on-disk directory.

    Fast path: check predicted_album_paths() against cached subdir listings.
    Fuzzy path: expand artist variants ('The X' / 'X' / comma-prefix) and
    scan each parent dir for the best-scoring album folder.
    """
    candidates = predicted_album_paths(qobuz_album)
    vlog(f"predicted {len(candidates)} candidate path(s)")

    _parent_kid_names: dict = {}
    for c in candidates:
        p_str = str(c.parent)
        if p_str not in _parent_kid_names:
            _parent_kid_names[p_str] = {
                k.name for k in _list_artist_subdirs_cached(c.parent)
            }
        if c.name in _parent_kid_names[p_str]:
            vlog(f"exact match: {c}")
            return c

    artist = (qobuz_album.get("artist") or {}).get("name") or ""
    title  = qobuz_album.get("title") or ""

    if not artist:
        return None

    artist_variants = [artist]
    if artist.lower().startswith("the "):
        artist_variants.append(artist[4:])
    else:
        artist_variants.append("The " + artist)
    if "," in artist:
        artist_variants.append(artist.split(",")[0].strip())

    search_dirs = []
    seen_dirs: set = set()

    def _push(p):
        key = str(p)
        if key in seen_dirs:
            return
        seen_dirs.add(key)
        search_dirs.append(p)

    for av in artist_variants:
        ad = config.MUSIC_ROOT / beets_sanitize(av)
        if ad.exists():
            _push(ad)
        for ma in _find_multi_artist_dirs(av):
            _push(ma)

    # Diacritics often differ between Qobuz and disk ('Beyoncé' vs 'Beyonce',
    # 'Motörhead' vs 'Motorhead'); the exact .exists() checks above miss those,
    # so also take any artist folder whose ASCII-folded name matches. The
    # per-album title-similarity gate below still guards against a wrong folder.
    artist_norms = {normalize(av) for av in artist_variants if normalize(av)}
    if artist_norms and config.MUSIC_ROOT.exists():
        for d in _list_artist_subdirs_cached(config.MUSIC_ROOT):
            if normalize(d.name) in artist_norms:
                _push(d)

    global_best, global_best_score = None, 0.0
    norm_title = normalize(strip_album_decorations(title))
    for artist_dir in search_dirs:
        vlog(f"scanning artist dir: {artist_dir}")
        subdirs = _list_artist_subdirs_cached(artist_dir)
        if not subdirs:
            continue

        stripped_title = strip_album_decorations(title)
        # Evaluate every subdir that clears both gates, not just the single
        # top scorer: a prefix-only high scorer that fails the coverage gate
        # (studio 'The North Borders' scoring 0.79 against the live 'The North
        # Borders Tour — Live') must not mask a lower-scored folder in the same
        # artist dir that is the real match.
        for d in subdirs:
            score = similarity(strip_album_decorations(d.name), stripped_title)
            if score < config.FUZZY_DIR_THRESH or score <= global_best_score:
                continue
            # Real edition variants normalize to the same string, so require the
            # titles to be close in length — that keeps an extra word ('Live',
            # 'Tour') from resolving to the base album's folder on prefix alone.
            norm_dir = normalize(strip_album_decorations(d.name))
            coverage = (min(len(norm_dir), len(norm_title))
                        / max(len(norm_dir), len(norm_title))
                        if norm_dir and norm_title else 0.0)
            if coverage < config.FUZZY_DIR_MIN_COVERAGE:
                vlog(f"rejected {d.name}: score {score:.2f} but length "
                     f"coverage {coverage:.2f} < {config.FUZZY_DIR_MIN_COVERAGE}")
                continue
            global_best, global_best_score = d, score
            vlog(f"fuzzy match: {d}  (score {score:.2f})")
    return global_best


def find_existing_tracks(qobuz_album, *, album_dir=None):
    """Return (track_list, album_dir_or_None) by reading the filesystem.

    track_list is empty when no album dir is found — surfaces as
    '0 of N present' so the user can spot a folder-naming mismatch.

    Pass ``album_dir`` when the caller already resolved it — the artist
    walk and the single-album download path both resolve once via
    ``find_album_dir_filesystem`` for a guardrail and would otherwise
    re-resolve here, doubling the cached-subdir-listing work per album.
    """
    if album_dir is None:
        album_dir = find_album_dir_filesystem(qobuz_album)
    if album_dir is None:
        return [], None
    return read_album_dir(album_dir), album_dir


# ── Track-presence detection ──────────────────────────────────────────────────
def _norm_isrc(value):
    """Canonical ISRC for identity comparison. A blank or whitespace-only tag
    must collapse to '' so two un-identified tracks aren't treated as the same
    recording (which would hide a real gap, or a bonus track before a wipe)."""
    return (value or "").strip().replace("-", "").upper()


def _track_key(title_raw, disc, isrc):
    """Identity keys for one track: ISRC plus the disc-scoped title in both its
    full and edition-stripped forms.

    Titles are compared normalized (ASCII-folded, punctuation dropped). A
    non-Latin title folds to '' under normalize, so fall back to the casefolded
    raw title — without it, "東京" and "大阪" would both key on '' and either
    match each other or nothing at all. A genuinely empty title leaves the key
    None so two untitled tracks never pair on an empty string.
    """
    def _scoped(text):
        norm = normalize(text) or text.strip().casefold()
        return (disc, norm) if norm else None

    title_raw = title_raw or ""
    return {
        "isrc": _norm_isrc(isrc),
        "title": _scoped(title_raw),
        "stripped": _scoped(strip_edition_suffix(title_raw)),
    }


# Strongest identity first: an ISRC-identifiable track claims its exact slot
# before a weaker title match can consume it. Reordering causes false
# re-downloads of already-owned tracks.
_MATCH_LAYERS = ("isrc", "title", "stripped")


def _keys_match(a, b, layer):
    """Whether two track keys agree at one matching layer. Symmetric in a/b;
    the 'stripped' layer pairs an edition-stripped title on either side, so
    'Foo (LP Version)' matches a bare 'Foo' and the reverse."""
    if layer == "isrc":
        return bool(a["isrc"]) and a["isrc"] == b["isrc"]
    if layer == "title":
        return a["title"] is not None and a["title"] == b["title"]
    return any(x is not None and x == y for x, y in (
        (a["title"], b["stripped"]),
        (a["stripped"], b["title"]),
        (a["stripped"], b["stripped"]),
    ))


def _pair_tracks(claim_keys, slot_keys):
    """One-to-one match claim_keys against slot_keys, strongest layer first.

    Returns a bool list over claim_keys marking which claimed a slot. Each slot
    is consumed once, so a repeated title (a reprise, a hidden track, two
    same-named versions) leaves the surplus unclaimed rather than letting them
    all match a single file — the multiplicity that keeps a real gap, or a bonus
    track before an upgrade wipe, from being silently hidden.
    """
    used = [False] * len(slot_keys)
    claimed = [False] * len(claim_keys)
    for layer in _MATCH_LAYERS:
        for ci, ck in enumerate(claim_keys):
            if claimed[ci]:
                continue
            for si, sk in enumerate(slot_keys):
                if not used[si] and _keys_match(ck, sk, layer):
                    used[si] = claimed[ci] = True
                    break
    return claimed


def _qobuz_keys(qobuz_tracks):
    return [_track_key(qt.get("title"), qt.get("media_number", 1) or 1,
                       qt.get("isrc")) for qt in qobuz_tracks]


def _existing_keys(existing_tracks):
    return [_track_key(et.get("title"), et.get("discnumber", 1) or 1,
                       et.get("isrc")) for et in existing_tracks]


def compute_missing(qobuz_tracks, existing_tracks):
    """Split qobuz_tracks into (missing, present) against existing_tracks.

    A Qobuz track is present when an on-disk track matches it on ISRC,
    normalized title, or edition-stripped title — in that priority. Matching is
    one-to-one (see _pair_tracks), so a duplicate title surfaces as a real gap
    instead of being masked by a single file.
    """
    claimed = _pair_tracks(_qobuz_keys(qobuz_tracks),
                           _existing_keys(existing_tracks))
    missing = [qt for i, qt in enumerate(qobuz_tracks) if not claimed[i]]
    present = [qt for i, qt in enumerate(qobuz_tracks) if claimed[i]]
    return missing, present


def find_extras_in_existing(qobuz_tracks, existing_tracks):
    """Return on-disk tracks that match no Qobuz track.

    The reverse of compute_missing: each Qobuz track accounts for at most one
    file, so a same-titled bonus track (a custom rip, a B-side, a deluxe extra)
    is flagged rather than hidden. This gates the upgrade wipe-and-replace, so a
    missed extra means silently deleting a track the user can't get back.
    """
    matched = _pair_tracks(_existing_keys(existing_tracks),
                           _qobuz_keys(qobuz_tracks))
    return [et for i, et in enumerate(existing_tracks) if not matched[i]]


# ── Album metadata helpers ────────────────────────────────────────────────────
def album_year(album):
    """Album release year as a string, or '' if unknown.

    Prefers release_date_original. Falls back to released_at parsed in UTC
    (not local timezone — local-TZ parsing was a real bug that flipped the
    year for albums released late at night UTC).
    """
    rdo = album.get("release_date_original") or ""
    if isinstance(rdo, str) and rdo[:4].isdigit():
        return rdo[:4]
    ra = album.get("released_at")
    if isinstance(ra, (int, float)):
        try:
            return str(datetime.fromtimestamp(int(ra), tz=timezone.utc).year)
        except (ValueError, OSError, OverflowError):
            return ""
    return ""


def album_year_int(album, fallback=99999):
    """Numeric year for sorting; missing → fallback (sorts last by default)."""
    y = album_year(album)
    try:
        return int(y) if y else fallback
    except ValueError:
        return fallback


def is_lossless_album(album):
    return (album.get("maximum_bit_depth") or 0) >= 16


def album_quality_label(album):
    """Human-readable quality string for a Qobuz album dict."""
    bd = album.get("maximum_bit_depth") or 0
    sr = album.get("maximum_sampling_rate") or 0
    sr_khz = (sr / 1000) if sr >= 1000 else sr
    if bd >= 24:
        return f"{bd}-bit/{sr_khz:g}kHz (hi-res)"
    if bd >= 16:
        return f"{bd}-bit/{sr_khz:g}kHz"
    return "lossy"


# ── Catalog filtering ─────────────────────────────────────────────────────────
_MIN_ALBUM_TRACKS = 4  # below this an edition is a stray single/EP, not the
                       # album — ignored when sizing the standard edition.


def _standard_track_count(group):
    """Track count of the real album among a group of editions.

    Deluxe/anniversary/expanded editions only ADD tracks and remasters keep
    the original count, so the smallest real edition is the album. None when
    no edition reports a usable count.
    """
    counts = [tc for a in group if (tc := a.get("tracks_count") or 0) > 0]
    if not counts:
        return None
    full = [c for c in counts if c >= _MIN_ALBUM_TRACKS]
    return min(full) if full else min(counts)


def _is_decorated_edition(album):
    """True if the title carries an edition tag (remaster, deluxe, anniversary,
    a year, ...) rather than being the plain original release."""
    title = album.get("title") or ""
    return (strip_album_decorations(title).casefold().strip()
            != title.casefold().strip())


def _best_edition(group, prefer_hires):
    """Representative edition for a group of same-album editions.

    Both modes target the standard track count so a padded deluxe/anniversary
    edition never wins on track count alone. prefer_hires then takes the best
    resolution at that count; otherwise it takes the original (untagged)
    edition without chasing a remaster's higher resolution.
    """
    standard = _standard_track_count(group)

    def off_standard(a):
        if standard is None:
            return 0
        return abs((a.get("tracks_count") or 0) - standard)

    if prefer_hires:
        return min(group, key=lambda a: (
            off_standard(a),
            -(a.get("maximum_bit_depth") or 0),
            -(a.get("maximum_sampling_rate") or 0),
            album_year_int(a),
            str(a.get("id") or ""),
        ))
    return min(group, key=lambda a: (
        off_standard(a),
        _is_decorated_edition(a),
        album_year_int(a),
        str(a.get("id") or ""),
    ))


def filter_owned_albums(catalog_pairs, owned_bare_titles):
    """Drop catalog entries whose bare title matches something already owned.

    Exact match (normalized bare title in owned_bare_titles) with a year-
    aware check: if all owned copies are >3 years away, treat as distinct.
    Falls back to fuzzy match at CONSOLIDATE_THRESH for edition variants the
    decoration stripper didn't fully normalize.
    """
    if not owned_bare_titles:
        return list(catalog_pairs)
    missing = []
    for album, n_versions in catalog_pairs:
        bare = strip_album_decorations(album.get("title") or "")
        key = normalize(bare)
        if not key:
            missing.append((album, n_versions))
            continue
        if key in owned_bare_titles:
            owned_years = owned_bare_titles[key]
            catalog_yr_str = album_year(album)
            try:
                catalog_yr = int(catalog_yr_str) if catalog_yr_str else None
            except ValueError:
                catalog_yr = None
            if catalog_yr is None or None in owned_years:
                continue
            if any(abs(oy - catalog_yr) <= 3 for oy in owned_years
                   if oy is not None):
                continue
            missing.append((album, n_versions))
            continue
        # Fuzzy fallback for edition variants the stripper didn't fully
        # normalize. Require the catalog title to be the owned one plus a
        # suffix (an un-stripped edition tag) AND a nearby year — without those
        # guards, sequels and numbered entries ('Load'/'Reload', 'Vol 1'/'Vol
        # 2', 'II'/'III') get silently hidden from the missing list as owned.
        catalog_yr_str = album_year(album)
        try:
            cat_yr = int(catalog_yr_str) if catalog_yr_str else None
        except ValueError:
            cat_yr = None
        owned_fuzzy = False
        for owned, owned_years in owned_bare_titles.items():
            if not owned or not (key.startswith(owned) or owned.startswith(key)):
                continue
            if similarity(key, owned) < config.CONSOLIDATE_THRESH:
                continue
            if (cat_yr is None or None in owned_years
                    or any(abs(oy - cat_yr) <= 3
                           for oy in owned_years if oy is not None)):
                owned_fuzzy = True
                break
        if owned_fuzzy:
            continue
        missing.append((album, n_versions))
    return missing


def filter_short_releases(catalog_pairs, min_tracks=None):
    """Drop releases with fewer than min_tracks tracks.
    Hides standalone singles and very short EPs from the missing-albums step
    by default. None/missing tracks_count is allowed through (don't penalize
    bad metadata).
    """
    if min_tracks is None:
        min_tracks = config.MISSING_ALBUMS_MIN_TRACKS
    kept = []
    for album, n_versions in catalog_pairs:
        tc = album.get("tracks_count")
        if tc is None:
            kept.append((album, n_versions))
            continue
        try:
            if int(tc) >= min_tracks:
                kept.append((album, n_versions))
        except (TypeError, ValueError):
            kept.append((album, n_versions))
    return kept


def dedup_album_versions(albums, prefer_hires=False):
    """Collapse multiple editions of the same album into one canonical entry.

    Two albums dedup if their stripped-decoration titles normalize identically.
    Within a group, picks (see _best_edition):
      - prefer_hires=True:  best resolution at the standard track count
      - prefer_hires=False: the original (untagged) edition
    The standard track count is the smallest real edition, so a padded
    deluxe/anniversary release never wins on track count alone.

    Returns list of (canonical_album, n_versions_in_group) tuples, sorted by
    canonical's release year ascending.
    """
    groups = {}
    for a in albums:
        bare = strip_album_decorations(a.get("title") or "")
        # Pure-CJK / emoji titles normalize to '' (ASCII-fold drops them);
        # fall back to the raw title so they aren't silently dropped from the
        # missing list. Only a genuinely empty title is skipped.
        key_title = normalize(bare) or bare.strip().casefold()
        if not key_title:
            continue
        # Include year in the group key. Without this, any trailing
        # parenthetical gets stripped — including (LP2), (LP3), (LP4) —
        # so all of a band's self-titled albums collapse into one dedup
        # group and only the earliest-year representative reaches the missing-albums step.
        # Albums with the same bare title AND the same year still dedup
        # correctly (studio album + same-year deluxe edition).
        key = (key_title, album_year_int(a))
        groups.setdefault(key, []).append(a)

    representatives = []
    for group in groups.values():
        best = _best_edition(group, prefer_hires)
        representatives.append((best, len(group)))

    representatives.sort(key=lambda pair: (
        album_year_int(pair[0]),
        normalize(pair[0].get("title", "")),
    ))
    return representatives


def _artist_name_sim(a, b):
    """Artist-name similarity that treats a leading 'The' as optional, so
    'The Beatles' and 'Beatles' match."""
    def _bare(s):
        s = (s or "").strip()
        return s[4:].strip() if s[:4].lower() == "the " else s
    return max(similarity(a, b), similarity(_bare(a), _bare(b)))


def filter_compilation_albums(catalog_pairs, artist_name):
    """Drop entries that look like compilations or have a different primary artist."""
    kept = []
    for album, n_versions in catalog_pairs:
        a_artist = (album.get("artist") or {}).get("name", "")
        if not a_artist:
            continue
        # If the album's primary artist isn't a strong match for the queried
        # artist, it's almost certainly a compilation/various-artists release.
        if _artist_name_sim(a_artist, artist_name) < config.ARTIST_NAME_THRESH:
            continue
        # Some Qobuz records flag compilations explicitly; honor it when present.
        if album.get("is_compilation") is True:
            continue
        kept.append((album, n_versions))
    return kept


# ── Post-import filesystem helpers ────────────────────────────────────────────

def maybe_remove_empty_dir(d: Path):
    """Remove dir only if it has no remaining files (cover art etc preserved)."""
    try:
        children = list(iter_tree_no_symlinks(d))
        if any(p.is_file() for p in children):
            return False
        # Remove empty subdirs deepest-first
        subdirs = sorted((p for p in children if p.is_dir()),
                         key=lambda p: -len(p.parts))
        for sd in subdirs:
            try: sd.rmdir()
            except OSError: pass
        d.rmdir()
        return True
    except OSError:
        return False


def _count_audio_files_in(d):
    """Count audio files recursively in a directory. 0 if missing."""
    if d is None or not d.exists():
        return 0
    n = 0
    try:
        for f in iter_tree_no_symlinks(d):
            if f.is_file() and f.suffix.lower() in config.AUDIO_EXTS:
                n += 1
    except OSError:
        pass
    return n


def cleanup_duplicate_art(album_dir: Path) -> int:
    """Remove duplicate cover-art files that beets created via .N.jpg suffix.

    When a partial album fill lands in a folder that already has cover art,
    beets resolves the filename collision by renaming the incoming file to
    cover.1.jpg, folder.2.png, etc. Net result: duplicate art files left
    behind. We delete them post-import.

    Pattern: <basename>.<digits>.<ext> where basename is in a known art-name
    set and ext is a known image extension. The unnumbered base file
    (<basename>.<ext>) MUST also exist — that's what makes it a beets
    collision, not user-curated multi-art (booklet scans named
    front.1/front.2 with no base front.<ext> are kept). Returns count of
    files removed.
    """
    if not album_dir or not album_dir.exists():
        return 0
    art_names = {"cover", "folder", "album", "front", "art", "artwork", "albumart"}
    art_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    pattern = re.compile(r"^(.+)\.\d+$")
    removed = 0
    try:
        names_present = {f.name.lower() for f in album_dir.iterdir() if f.is_file()}
    except OSError:
        return 0
    try:
        for f in album_dir.iterdir():
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext not in art_exts:
                continue
            m = pattern.match(f.stem)
            if not m:
                continue
            base = m.group(1).lower()
            if base not in art_names:
                continue
            # Only delete the numbered variant when the unnumbered base
            # file exists too — that's the beets collision signature. If
            # the user has cover.1.jpg / cover.2.jpg with no cover.jpg,
            # those are their booklet scans; leave them.
            if f"{base}{ext}" not in names_present:
                continue
            try:
                f.unlink()
                removed += 1
                vlog(f"removed duplicate art: {f.name}")
            except OSError:
                pass
    except OSError:
        pass
    return removed


def _prompt_migration_conflict(src_dir: Path, dest_dir: Path, args):
    """Two albums for the same Qobuz release exist — one in the multi-artist
    folder we just imported into, one in the primary-artist folder. Show
    track count + quality side-by-side and ask whether to merge.

    Returns True if the user wants to merge (file-by-file move with collision
    prompts handled in `_merge_album_dirs`); False to leave both folders."""
    from qobuz_librarian.library.scanner import read_album_dir as _read_album_dir
    from qobuz_librarian.quality.tiers import format_quality
    from qobuz_librarian.ui_cli.prompts import confirm

    src_tracks = _read_album_dir(src_dir)
    dst_tracks = _read_album_dir(dest_dir)

    def _q(tracks):
        if not tracks:
            return "(empty)"
        bits = max((t.get("bits") or 0) for t in tracks)
        rate = max((t.get("sample_rate") or 0) for t in tracks)
        return format_quality(bits, rate) if (bits or rate) else "?"

    print()
    log.info(fmt(C.YELLOW + C.BOLD,
        "  ⚠  Multi-artist migration conflict"))
    log.info(fmt(C.GRAY,
        "     A folder for this album already exists at the primary-artist "
        "location."))
    log.info(fmt(C.WHITE,
        f"     [src]  {src_dir}"))
    log.info(fmt(C.GRAY,
        f"            {len(src_tracks)} track(s) · {_q(src_tracks)}"))
    log.info(fmt(C.WHITE,
        f"     [dst]  {dest_dir}"))
    log.info(fmt(C.GRAY,
        f"            {len(dst_tracks)} track(s) · {_q(dst_tracks)}"))
    log.info(fmt(C.GRAY,
        "     Merge moves missing tracks from [src] → [dst]; on per-file "
        "conflicts you'll be asked to keep src or dst."))
    return confirm(
        "  Merge [src] into [dst]?",
        default_yes=True, auto_yes=False)


# Beyond this many nested subdirs we stop merging. A real "Disc N" or
# "CD N" structure is one level. Pathological inputs (symlink loop,
# user-confused 20-level deep tree) would otherwise blow the stack or
# spin forever.
_MERGE_MAX_DEPTH = 8


def _merge_album_dirs(src: Path, dst: Path, _depth: int = 0) -> bool:
    """Move src/* into dst/. For each conflict, ask the user which to keep,
    showing size + quality. Recurses into directory-vs-directory collisions
    (multi-disc albums with Disc 1/, Disc 2/ subdirs). Returns True on
    completion (even if some files were skipped); False on a hard I/O failure."""
    import shutil as _shutil

    from qobuz_librarian.ui_cli.colors import format_size
    from qobuz_librarian.ui_cli.prompts import confirm

    if _depth >= _MERGE_MAX_DEPTH:
        log.info(fmt(C.YELLOW,
            f"     skipped {src.name}/: nesting deeper than "
            f"{_MERGE_MAX_DEPTH} levels (suspected loop or pathological tree)"))
        return False

    try:
        items = sorted(src.iterdir())
    except OSError as e:
        log.info(fmt(C.RED, f"  ✗  Couldn't list {src}: {e}."))
        return False

    for item in items:
        target = dst / item.name
        if not target.exists():
            try:
                _shutil.move(str(item), str(target))
            except OSError as e:
                log.info(fmt(C.YELLOW, f"     skipped {item.name}: {e}"))
            continue

        # Directory-vs-directory collision (typically multi-disc Disc N/):
        # recurse so per-file conflict prompts still happen at leaf level.
        if item.is_dir() and target.is_dir():
            # Refuse to follow symlinked subdirs — they're how a single
            # loop turns into infinite recursion. Real multi-disc albums
            # don't use symlinks for disc folders.
            if item.is_symlink() or target.is_symlink():
                log.info(fmt(C.YELLOW,
                    f"     skipped {item.name}/: symlinked subdir"))
                continue
            log.info(fmt(C.GRAY, f"     merging subdir: {item.name}/"))
            _merge_album_dirs(item, target, _depth=_depth + 1)
            # After recursion, src subdir should be empty (all files moved
            # or dropped). Try to remove it; ignore failure (means user kept
            # something there).
            try:
                item.rmdir()
            except OSError:
                pass
            continue

        # Mismatched type (file vs dir) — refuse rather than guess.
        if item.is_dir() != target.is_dir():
            log.info(fmt(C.YELLOW,
                f"     skipped {item.name}: type mismatch (file vs dir)"))
            continue

        # File-vs-file collision. Compare and ask. Only prompt once per file.
        try:
            src_sz = item.stat().st_size
            dst_sz = target.stat().st_size
        except OSError:
            src_sz = dst_sz = 0
        log.info(fmt(C.WHITE, f"     conflict: {item.name}"))
        log.info(fmt(C.GRAY,
            f"       src: {format_size(src_sz)}   dst: {format_size(dst_sz)}"))
        if confirm("       Replace dst with src?",
                   default_yes=False, auto_yes=False):
            try:
                item.replace(target)
            except OSError as e:
                log.info(fmt(C.YELLOW, f"       failed: {e}"))
        else:
            try:
                item.unlink()
            except OSError as e:
                log.info(fmt(C.YELLOW, f"       couldn't drop src: {e}"))
    return True


def _sync_beets_db_after_move(old_dir: Path, new_dir: Path) -> None:
    """Update beets' items.path after a directory move performed outside
    beets' control. Without this, the next `beet update` marks every
    track under old_dir as deleted because the relative path stored in
    items.path still points at the pre-move location.

    Failures are surfaced but never raised — a sync slip is recoverable
    (`beet update`), an exception here would unwind the migration and
    leave the disk and DB in worse drift than before.
    """
    db_path = getattr(config, "BEETS_DB_PATH", None)
    if not db_path:
        return
    db_path = Path(str(db_path))
    if not db_path.exists():
        return
    music_root = Path(str(config.MUSIC_ROOT))
    try:
        old_rel = old_dir.resolve().relative_to(music_root.resolve())
        new_rel = new_dir.resolve().relative_to(music_root.resolve())
    except (ValueError, OSError):
        # Not under MUSIC_ROOT — beets doesn't track these.
        return
    old_prefix = str(old_rel).encode("utf-8") + b"/"
    new_prefix = str(new_rel).encode("utf-8") + b"/"
    import sqlite3
    try:
        with sqlite3.connect(str(db_path)) as conn:
            # items.path is BLOB, relative to beets `directory`. Replace
            # only the prefix; keep the per-disc/per-track suffix intact.
            # `||` coerces BLOB → TEXT, so wrap the concat in CAST AS BLOB
            # to preserve the byte-string type beets expects.
            conn.execute(
                "UPDATE items SET path = CAST(? || SUBSTR(path, ?) AS BLOB) "
                "WHERE SUBSTR(path, 1, ?) = ?",
                (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix),
            )
            conn.commit()
    except sqlite3.Error as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  beets DB path sync failed ({e}). "
            "Run `beet update` to re-scan."))


def _sync_beets_db_after_merge(old_dir: Path, new_dir: Path) -> None:
    """Like _sync_beets_db_after_move, but for the merge path where new_dir
    already holds rows. Repoint each source row to its new path — except where a
    row already exists at that path (a file collision the merge resolved), where
    the source row is dropped instead. A blind prefix rewrite would otherwise
    leave two items rows pointing at one file and break the next `beet update`.
    """
    db_path = getattr(config, "BEETS_DB_PATH", None)
    if not db_path:
        return
    db_path = Path(str(db_path))
    if not db_path.exists():
        return
    music_root = Path(str(config.MUSIC_ROOT))
    try:
        old_rel = old_dir.resolve().relative_to(music_root.resolve())
        new_rel = new_dir.resolve().relative_to(music_root.resolve())
    except (ValueError, OSError):
        return
    old_prefix = str(old_rel).encode("utf-8") + b"/"
    new_prefix = str(new_rel).encode("utf-8") + b"/"
    import sqlite3
    try:
        with sqlite3.connect(str(db_path)) as conn:
            # Drop source rows whose target path already has a row (collision).
            conn.execute(
                "DELETE FROM items WHERE SUBSTR(path, 1, ?) = ? AND EXISTS ("
                " SELECT 1 FROM items b "
                " WHERE b.path = CAST(? || SUBSTR(items.path, ?) AS BLOB))",
                (len(old_prefix), old_prefix, new_prefix, len(old_prefix) + 1),
            )
            # Repoint the rest (no pre-existing row at the new path).
            conn.execute(
                "UPDATE items SET path = CAST(? || SUBSTR(path, ?) AS BLOB) "
                "WHERE SUBSTR(path, 1, ?) = ?",
                (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix),
            )
            conn.commit()
    except sqlite3.Error as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  beets DB merge sync failed ({e}). "
            "Run `beet update` to re-scan."))


def _sync_beets_db_after_file_move(old_file: Path, new_file: Path) -> None:
    """Update beets' items.path for one file moved outside beets' control.

    Same rationale as _sync_beets_db_after_move, but matches a single exact
    path rather than a directory prefix — used when a repaired track beets
    filed under its tag-derived album is relocated into the folder actually
    being repaired.
    """
    db_path = getattr(config, "BEETS_DB_PATH", None)
    if not db_path:
        return
    db_path = Path(str(db_path))
    if not db_path.exists():
        return
    music_root = Path(str(config.MUSIC_ROOT))
    try:
        old_rel = old_file.resolve().relative_to(music_root.resolve())
        new_rel = new_file.resolve().relative_to(music_root.resolve())
    except (ValueError, OSError):
        return
    import sqlite3
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "UPDATE items SET path = ? WHERE path = ?",
                (str(new_rel).encode("utf-8"), str(old_rel).encode("utf-8")),
            )
            conn.commit()
    except sqlite3.Error as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  beets DB path sync failed ({e}). "
            "Run `beet update` to re-scan."))


def prompt_and_migrate_multi_artist_folder(album, args):
    """If `album` landed in a multi-artist folder ('Primary, Other'), move
    it under 'Primary'. No-op when:
      - Qobuz artist is a compilation alias ('Various Artists' etc.)
      - The album is already directly under the primary
      - The current folder isn't a recognised multi-artist form
      - The destination album folder exists and the user declines to merge

    Returns the resulting album dir Path (possibly unchanged) or None when
    we can't locate the folder.
    """
    import shutil as _shutil

    from qobuz_librarian.library.scanner import clear_scan_caches as _clear_caches
    from qobuz_librarian.library.tags import beets_sanitize, normalize

    qartist_raw = (album.get("artist") or {}).get("name") or ""
    if not qartist_raw:
        return None
    if qartist_raw.lower().strip() in (
            "various artists", "various", "va", "compilations", "soundtrack"):
        return find_album_dir_filesystem(album)

    cur = find_album_dir_filesystem(album)
    if cur is None:
        return None

    # Use the primary (first) name from the Qobuz artist string. Qobuz
    # returns "Jay Z and Kanye West" on some editions and "Jay Z" on
    # others; the folder is always named from the comma-joined track-
    # level form, so the migration target is the first name either way.
    qartist = _primary_artist_of(qartist_raw)

    cur_parent = cur.parent
    primary = beets_sanitize(qartist)
    if not primary:
        return cur

    # Already under the primary — nothing to do.
    if normalize(cur_parent.name) == normalize(primary):
        return cur

    # Only migrate when the parent is 'qartist, other' (comma-space form).
    # Other separators (' & ', ' and ') frequently appear inside real band
    # names so we don't auto-migrate those.
    if not _is_migration_candidate(cur_parent.name, qartist):
        return cur

    new_parent = config.MUSIC_ROOT / primary
    new_dir = new_parent / cur.name

    if new_dir.exists():
        if not _prompt_migration_conflict(cur, new_dir, args):
            return cur
        # User chose to merge: handle file-by-file, prompting on collisions.
        ok = _merge_album_dirs(cur, new_dir)
        if ok:
            _sync_beets_db_after_merge(cur, new_dir)
            maybe_remove_empty_dir(cur)
            maybe_remove_empty_dir(cur_parent)
            _clear_caches()
            return new_dir
        return cur

    try:
        new_parent.mkdir(parents=True, exist_ok=True)
        _shutil.move(str(cur), str(new_dir))
        # shutil.move falls back to copy+rmtree across filesystem
        # boundaries; on permission edge cases that has been observed
        # to leave files at the source while the rename log line
        # already fired. Sweep anything left behind into the new dir.
        if cur.exists():
            leftovers = [p for p in cur.rglob("*") if p.is_file()]
            if leftovers:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  {len(leftovers)} file(s) left at {cur} after move; "
                    "sweeping manually."))
                for src in leftovers:
                    rel = src.relative_to(cur)
                    dst = new_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        _shutil.move(str(src), str(dst))
            maybe_remove_empty_dir(cur)
        # Sync beets DB BEFORE the cache clear so a concurrent scan can't
        # see the new layout with the DB still pointing at the old path.
        _sync_beets_db_after_move(cur, new_dir)
        log.info(fmt(C.GRAY,
            f"  ⤷  Moved into primary-artist folder: {new_dir.name} "
            f"(parent: {primary})"))
        maybe_remove_empty_dir(cur_parent)
        _clear_caches()
        return new_dir
    except OSError as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't migrate to primary-artist folder: {e}."))
        return cur


# ── Qobuz catalog matching ────────────────────────────────────────────────────

def _catalog_candidates_for_dir(album_dir, catalog, artist_name, prefer_hires=False):
    """All catalog entries scoring above ARTIST_DIR_MATCH_THRESH for
    album_dir, sorted best-first (similarity + year-proximity bonus, lossless
    only, artist-name guard). Returning the ranked list — not just the top
    pick — lets find_qobuz_album_for_dir resolve the target_dir case from the
    catalog instead of falling back to the search API."""
    if not catalog:
        return []
    bare = strip_album_decorations(album_dir.name)
    bare_softened = bare.replace("_", " ").replace("  ", " ").strip()
    _ym = (re.search(r"\((\d{4})\)", album_dir.name)
           or re.search(r"\b(19\d{2}|20\d{2})\b", album_dir.name))
    dir_year = int(_ym.group(1)) if _ym else None

    candidates = []
    for r in catalog:
        if not is_lossless_album(r):
            continue
        r_artist = (r.get("artist") or {}).get("name", "")
        if r_artist and similarity(r_artist, artist_name) < config.ARTIST_NAME_THRESH:
            continue
        r_bare = strip_album_decorations(r.get("title", ""))
        s1 = similarity(r_bare, bare)
        s2 = similarity(r_bare, bare_softened) if bare_softened and bare_softened != bare else 0.0
        score = max(s1, s2)
        year_bonus = 0.0
        if dir_year is not None:
            ry_str = album_year(r)
            try:
                ry = int(ry_str) if ry_str else None
            except ValueError:
                ry = None
            if ry is not None:
                if ry == dir_year:
                    year_bonus = 0.10
                elif abs(ry - dir_year) <= 1:
                    year_bonus = 0.05
        candidates.append((score, year_bonus, r))

    if not candidates:
        return []
    if prefer_hires:
        candidates.sort(key=lambda x: (
            -(x[0] + x[1]),
            -((x[2].get("maximum_bit_depth") or 0)),
            -((x[2].get("maximum_sampling_rate") or 0)),
        ))
    else:
        candidates.sort(key=lambda x: -(x[0] + x[1]))
    return [r for (s, _, r) in candidates if s >= config.ARTIST_DIR_MATCH_THRESH]


def match_dir_to_catalog(album_dir, catalog, artist_name, prefer_hires=False):
    """Best single candidate from the artist catalog (or None)."""
    cands = _catalog_candidates_for_dir(album_dir, catalog, artist_name, prefer_hires)
    if not cands:
        return None
    best = cands[0]
    vlog(f"    local catalog match: {best.get('title')!r}")
    return best


def _pick_best_target_dir_match(scored_cands, target_dir):
    """Among candidates whose predicted on-disk path resolves to target_dir,
    pick the one whose tracks_count best fits the on-disk audio count.

    scored_cands: iterable of (combined_score: float, album_dict).
    Returns (chosen_album, chosen_score) or (None, 0).

    Fitting on track count keeps a 28-track anniversary box set with a bonus
    live disc from outranking the standard 11-track album when target_dir holds
    11 audio files — picking on quality alone there lists 'missing tracks' from
    a completely different edition.
    """
    n_disk = _count_audio_files_in(target_dir)
    resolving = []
    for score, cand in scored_cands:
        try:
            predicted = find_album_dir_filesystem(cand)
        except Exception:
            predicted = None
        if predicted is not None and _paths_equal(predicted, target_dir):
            resolving.append((score, cand))
    if not resolving:
        return None, 0
    if n_disk == 0:
        # Empty folder — nothing to fit against; trust score order.
        return resolving[0][1], resolving[0][0]

    def rank(item):
        score, cand = item
        tc = cand.get("tracks_count") or 0
        if tc == 0:
            return (10 ** 6, -score)  # unknown count → last resort
        if tc >= n_disk:
            dist = tc - n_disk
        else:
            # Slight bias toward tc >= n_disk so a partial library (5/11)
            # still picks the 11-track edition over a 5-track release.
            dist = (n_disk - tc) * 1.5
        return (dist, -score)

    resolving.sort(key=rank)
    return resolving[0][1], resolving[0][0]


def find_qobuz_album_for_dir(album_dir: Path, artist_name: str, token,
                             prefer_hires=False, catalog=None, target_dir=None):
    """Search Qobuz for the album matching an existing dir. Returns a fully-
    populated album dict (with track list) or None.

    Picks the highest-similarity match between the dir's bare name (stripped of
    year/edition decorations) and the candidate's bare title. Requires the
    candidate's artist name to match the queried artist (similarity >=
    ARTIST_NAME_THRESH 0.85) so e.g. "Live at Pompeii" by The Beatles doesn't
    match a Pink Floyd record. Lossless required.

    Builds multiple search queries to handle beets-sanitized characters (e.g.
    "Album_ Deluxe" was originally "Album: Deluxe" on Qobuz — searching with
    the literal underscore demotes the deluxe edition out of the top results).

    If a pre-fetched catalog is supplied, tries local matching first (zero
    search-API cost). Falls back to per-folder search only when the local
    match scores below threshold — handles compilation appearances and
    folders for albums beyond ARTIST_CATALOG_LIMIT.

    target_dir: when set, iterate all viable candidates (not just the top one)
    and return the first whose predicted on-disk path resolves back to
    target_dir. This lets sibling folders like "American Beauty (1970)" find
    the original Qobuz edition rather than always resolving to the hi-res
    deluxe edition that maps to a different folder. The catalog fast-path is
    skipped when target_dir is set because it returns only one candidate.
    """
    # Fast path: try pre-fetched catalog first.
    # Catalog walk now also handles the target_dir case so
    # upgrade walk doesn't fall back to the per-folder search API path
    # for every single album by an artist (huge perf win on long catalogs).
    if catalog:
        if target_dir is None:
            local = match_dir_to_catalog(album_dir, catalog, artist_name, prefer_hires)
            if local is not None:
                try:
                    return get_album(local["id"], token)
                except QobuzError as e:
                    log.info(fmt(C.YELLOW,
                        f"    ⚠  Catalog match get_album failed for "
                        f"album_id={local.get('id')} ({e}); trying per-folder search."))
        else:
            cands = _catalog_candidates_for_dir(
                album_dir, catalog, artist_name, prefer_hires)
            # Pick by tracks_count fit (not first-resolving).
            # Otherwise a 28-track anniversary edition with a bonus
            # live disc outranks the 11-track standard album when
            # both fuzzy-resolve to the same folder.
            scored = [(1.0 - 0.001 * i, c) for i, c in enumerate(cands)]
            best, _bs = _pick_best_target_dir_match(scored, target_dir)
            if best is not None:
                vlog(f"    catalog target-dir match: {best.get('title')!r} "
                     f"(tc={best.get('tracks_count')})")
                try:
                    return get_album(best["id"], token)
                except QobuzError as e:
                    log.info(fmt(C.YELLOW,
                        f"    ⚠  Catalog target-dir match failed for "
                        f"album_id={best.get('id')} ({e}); trying per-folder search."))

    bare = strip_album_decorations(album_dir.name)

    # Build query variants. Underscore is how beets sanitizes :, /, etc;
    # replacing it with space matches Qobuz's actual titles much better.
    queries = [f"{artist_name} {bare}".strip()]
    softened = bare.replace("_", " ").replace("  ", " ").strip()
    if softened and softened != bare:
        queries.append(f"{artist_name} {softened}".strip())
    # Last-resort fallback: prefix only (everything before first sanitized char).
    # Lets us find "Juturna: Deluxe..." by searching "Circa Survive Juturna".
    prefix = re.split(r"[_:]", bare, maxsplit=1)[0].strip()
    if prefix and prefix != bare and len(prefix) >= 3:
        queries.append(f"{artist_name} {prefix}".strip())

    results = []
    seen_ids = set()
    for q in queries:
        try:
            batch = search_albums(q, token, limit=config.CATALOG_SEARCH_LIMIT)
        except QobuzError as e:
            vlog(f"    search variant {q!r} failed: {e}")
            continue
        for r in batch:
            rid = r.get("id")
            if rid and rid not in seen_ids:
                seen_ids.add(rid)
                results.append(r)
    if not results:
        return None

    # Year extracted from dir name for the same self-titled-tie reason as
    # match_dir_to_catalog. See that function for the rationale.
    _ym = re.search(r"\((\d{4})\)", album_dir.name) or re.search(r"\b(19\d{2}|20\d{2})\b", album_dir.name)
    dir_year = int(_ym.group(1)) if _ym else None

    # Filter to lossless + matching artist
    candidates = []
    for r in results:
        if not is_lossless_album(r):
            continue
        r_artist = (r.get("artist") or {}).get("name", "")
        if similarity(r_artist, artist_name) < config.ARTIST_NAME_THRESH:
            continue
        r_bare = strip_album_decorations(r.get("title", ""))
        score = similarity(r_bare, bare)
        # Year-proximity bonus: at most +0.10 (exact) or +0.05 (within 1 yr).
        year_bonus = 0.0
        if dir_year is not None:
            ry_str = album_year(r)
            try:
                ry = int(ry_str) if ry_str else None
            except ValueError:
                ry = None
            if ry is not None:
                if ry == dir_year:
                    year_bonus = 0.10
                elif abs(ry - dir_year) <= 1:
                    year_bonus = 0.05
        candidates.append((score, year_bonus, r))

    if not candidates:
        vlog(f"    no candidates after artist+lossless filter for {album_dir.name!r}")
        return None

    # Tie-break by year-proximity, then quality if --prefer-hires
    if prefer_hires:
        candidates.sort(key=lambda x: (
            -(x[0] + x[1]),
            -((x[2].get("maximum_bit_depth") or 0)),
            -((x[2].get("maximum_sampling_rate") or 0)),
        ))
    else:
        candidates.sort(key=lambda x: -(x[0] + x[1]))

    best_score, best_year_bonus, best = candidates[0]
    if best_score < config.ARTIST_DIR_MATCH_THRESH:
        vlog(f"    best Qobuz match {best.get('title')!r} scored {best_score:.2f} — under threshold")
        return None

    # When a specific on-disk target is required, walk all viable candidates
    # and return the first whose predicted path resolves back to target_dir.
    # This handles sibling folders (e.g. "American Beauty (1970)") where the
    # top hit is a deluxe edition that maps to a different on-disk folder —
    # we keep looking until we find a version that actually belongs here.
    if target_dir is not None:
        # Pick among the viable candidates by track-count fit, not score, so a
        # box set doesn't outrank the standard album the folder actually holds.
        viable = [(s + yb, r) for (s, yb, r) in candidates
                  if s >= config.ARTIST_DIR_MATCH_THRESH]
        best, best_score = _pick_best_target_dir_match(viable, target_dir)
        if best is not None:
            vlog(f"    Qobuz match (target-dir aligned): {best.get('title')!r} "
                 f"(score {best_score:.2f}, tc={best.get('tracks_count')})")
            try:
                return get_album(best["id"], token)
            except QobuzError as e:
                log.info(fmt(C.YELLOW, f"    ⚠  Couldn't fetch album details: {e}."))
                return None
        vlog(f"    no candidate resolves to {target_dir.name!r}; returning None")
        return None

    vlog(f"    Qobuz match: {best.get('title')!r} (score {best_score:.2f})")

    # Pull the full track list
    try:
        return get_album(best["id"], token)
    except QobuzError as e:
        log.info(fmt(C.YELLOW, f"    ⚠  Couldn't fetch album details: {e}."))
        return None


def find_expanded_edition(album, album_dir, existing, token, args):
    """When a Qobuz edition produces extras that would block an upgrade, search
    for alternate editions whose track count covers the local library.

    Returns a list of (full_album_dict, new_extras_list) tuples for editions
    that don't make the extras-blocking-upgrade situation worse. Sorted: fewest
    new_extras first (zero is best — covers all local tracks), then by quality
    descending ('quality is king'), then by track count descending.

    Limits to 3 full get_album() calls to keep API usage sane. Returns []
    (empty list) if nothing viable is found.
    """
    if not token or album_dir is None:
        return []

    artist_name = (album.get("artist") or {}).get("name", "") or ""
    bare = strip_album_decorations(album.get("title") or "")
    if not bare or not artist_name:
        return []

    n_local = len(existing)

    # Build the same query variants as find_qobuz_album_for_dir.
    queries = [f"{artist_name} {bare}".strip()]
    softened = bare.replace("_", " ").replace("  ", " ").strip()
    if softened != bare:
        queries.append(f"{artist_name} {softened}".strip())
    prefix = re.split(r"[_:]", bare, maxsplit=1)[0].strip()
    if prefix and prefix != bare and len(prefix) >= 3:
        queries.append(f"{artist_name} {prefix}".strip())

    seen_ids = {album.get("id")}  # skip the edition we already have
    candidates = []
    for q in queries:
        try:
            batch = search_albums(q, token, limit=config.CATALOG_SEARCH_LIMIT)
        except QobuzError:
            continue
        for r in batch:
            rid = r.get("id")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            if not is_lossless_album(r):
                continue
            r_artist = (r.get("artist") or {}).get("name", "") or ""
            if similarity(r_artist, artist_name) < config.ARTIST_NAME_THRESH:
                continue
            r_bare = strip_album_decorations(r.get("title") or "")
            score = similarity(r_bare, bare)
            if score < config.ARTIST_DIR_MATCH_THRESH:
                continue
            tc = r.get("tracks_count") or 0
            # Must have enough tracks to plausibly cover local collection.
            if tc < n_local - 1:
                continue
            candidates.append(r)

    if not candidates:
        return []

    # Sort by quality descending (quality is king), then track count descending.
    # We fetch full details in this order so the API budget goes to the most
    # likely-best candidates first.
    candidates.sort(key=lambda r: (
        -(r.get("maximum_bit_depth") or 0),
        -(r.get("maximum_sampling_rate") or 0),
        -(r.get("tracks_count") or 0),
    ))

    current_extras_count = len(find_extras_in_existing(
        (album.get("tracks") or {}).get("items") or [], existing))

    found = []
    api_calls = 0
    for cand in candidates:
        if api_calls >= config.EDITION_SEARCH_API_BUDGET:
            break
        try:
            full = get_album(cand["id"], token)
            api_calls += 1
        except QobuzError:
            continue
        q_tracks = (full.get("tracks") or {}).get("items") or []
        if not q_tracks:
            continue
        new_extras = find_extras_in_existing(q_tracks, existing)
        # Keep candidates that are no worse than current on the extras axis.
        # An equal-extras candidate at higher quality is still a useful pick
        # (e.g. the same deluxe edition mastered at 24/192 vs 16/44.1).
        if len(new_extras) <= current_extras_count:
            found.append((full, new_extras))
            vlog(f"    candidate edition: {full.get('title')!r} "
                 f"({len(new_extras)} extras vs {current_extras_count}, "
                 f"{album_quality_label(full)})")

    # Sort returned candidates: fewest new_extras first (zero is best),
    # then quality descending, then track count descending.
    found.sort(key=lambda fe: (
        len(fe[1]),
        -(fe[0].get("maximum_bit_depth") or 0),
        -(fe[0].get("maximum_sampling_rate") or 0),
        -len((fe[0].get("tracks") or {}).get("items") or []),
    ))
    return found
