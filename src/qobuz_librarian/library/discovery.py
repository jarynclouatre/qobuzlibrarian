"""The one engine behind "given an artist, what am I missing?".

Both faces call this: the CLI artist mode wraps it in terminal prompts, the web
scans turn its result into review candidates. Neither computes matching logic of
its own any more, so a fix lands once and the two can't drift.

The answer is built in two passes, because the right question differs for albums
you own versus albums you don't:

  Pass A — disk walk. For every folder you own under the artist, match it to the
    Qobuz edition that folder actually is and compare track-for-track. This is
    what catches gaps accurately, including in deluxe/anniversary editions and in
    singles the catalog-level filters drop.
  Pass B — catalog walk. Everything in the artist's catalog that Pass A didn't
    already account for, isn't owned under another folder, and isn't dismissed,
    surfaces as a fully-missing album.

`resolve_artist` lives here too: it's the shared artist matcher (article-aware,
prefers the deepest catalog over a bare-name twin, cached to disk).
"""
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost, QobuzError
from qobuz_librarian.api.search import get_album, get_artist_albums, search_artists
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.catalog import (
    _paths_equal,
    compute_missing,
    dedup_album_versions,
    filter_compilation_albums,
    filter_owned_albums,
    filter_short_releases,
    find_album_dir_filesystem,
    find_existing_tracks,
    find_qobuz_album_for_dir,
    is_lossless_album,
)
from qobuz_librarian.library.scanner import (
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_librarian.library.tags import normalize, similarity
from qobuz_librarian.ui_cli.logging import log, vlog

# ── Artist resolution (the shared matcher) ──────────────────────────────────────

_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def _strip_leading_article(name):
    """Drop a leading 'the/a/an ' so 'The Beatles' compares equal to a bare
    'Beatles'. Left unchanged if stripping would empty it."""
    return _LEADING_ARTICLE_RE.sub("", name or "", count=1) or name


# Folder name → matched Qobuz artist, cached to disk. Resolution is deterministic
# and artist ids are stable, so a re-scan skips the search call for every artist
# already matched — the slow half of a library scan. Misses are NOT cached (a
# later scan retries, in case the artist appears on Qobuz). Bump the version when
# the matching logic changes so stale matches drop; delete the file to re-resolve.
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
    """Return (artist_id, artist_name) for the best Qobuz match, or (None, None).

    Qobuz lists the canonical artist ('The Beatles') next to bare-name twins
    ('Beatles') that aggregate covers, interviews and bootlegs. A raw string
    match favours the twin — it has no 'The ' to cost it similarity — so compare
    with the leading article stripped and, among equally close names, take the
    one with the deepest catalog: the real artist's, not the twin's.

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


def resolve_artist_dir(artist_query):
    """Fuzzy-find an artist's directory in MUSIC_ROOT. Returns Path or None.
    Handles 'The X' / 'X' equivalence."""
    if not artist_query:
        return None
    candidates = list_library_artists()
    if not candidates:
        return None

    target = normalize(artist_query)
    if not target:
        return None

    for d in candidates:
        if normalize(d.name) == target:
            return d

    target_alt = target[3:] if target.startswith("the") else "the" + target
    for d in candidates:
        if normalize(d.name) == target_alt:
            return d

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


# ── The discovery result ────────────────────────────────────────────────────────

@dataclass
class DiscoveryOpts:
    prefer_hires: bool = False
    include_comps: bool = False
    include_singles: bool = False


@dataclass
class AlbumGap:
    """One album that needs attention. on_disk_dir is None for a fully-missing
    album; set (with missing/present track lists) for an owned album with gaps."""
    qobuz_album: dict
    on_disk_dir: Path | None
    missing: list = field(default_factory=list)
    present: list = field(default_factory=list)

    @property
    def fully_missing(self) -> bool:
        return self.on_disk_dir is None

    @property
    def missing_count(self) -> int:
        return len(self.missing)


@dataclass
class DiscoveryResult:
    artist_id: str | None
    artist_name: str | None
    gaps: list = field(default_factory=list)          # AlbumGap (partials + fully-missing)
    complete: list = field(default_factory=list)      # {dir, qobuz_album, existing}
    skipped: list = field(default_factory=list)       # {dir, reason, qobuz_title, ...}
    unmatched_dirs: list = field(default_factory=list)  # folders no Qobuz album matched
    catalog: list = field(default_factory=list)       # the fetched catalog (callers may reuse)
    catalog_truncated: bool = False

    @property
    def partials(self):
        return [g for g in self.gaps if g.on_disk_dir is not None]

    @property
    def fully_missing(self):
        return [g for g in self.gaps if g.on_disk_dir is None]


_YEAR_RE_PAREN = re.compile(r"\((\d{4})\)")
_YEAR_RE_BARE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def _folder_year(name):
    m = _YEAR_RE_PAREN.search(name) or _YEAR_RE_BARE.search(name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _record_owned_title(owned_titles, album_dir):
    from qobuz_librarian.library.tags import strip_album_decorations
    key = normalize(strip_album_decorations(album_dir.name))
    if key:
        owned_titles.setdefault(key, set()).add(_folder_year(album_dir.name))


def owned_album_titles(album_dirs):
    """{normalized bare title: {years}} for a set of album folders — the
    year-aware owned set the missing pass uses as a resolution-miss backstop.
    Shared so the CLI gap-fill and the engine seed it identically."""
    titles: dict = {}
    for ad in album_dirs:
        _record_owned_title(titles, ad)
    return titles


def _owned_by_name(owned_titles, album):
    """True when the album's bare title (year-aware) already names an owned
    folder — the backstop for a resolution miss, so a duplicate isn't offered."""
    return not filter_owned_albums([(album, 1)], owned_titles)


def _is_hidden(hidden, artist_name, album):
    return hidden is not None and hidden_mod.is_hidden(
        hidden_mod.SCOPE_MISSING, artist_name, album.get("title"), hidden)


def _materialize_tracks(album, token):
    """Ensure the album dict carries its track list (get_artist_albums omits it).
    Returns (album, tracks); album may be the freshly fetched full dict."""
    tracks = (album.get("tracks") or {}).get("items") or []
    if tracks:
        return album, tracks
    try:
        full = get_album(album.get("id"), token)
    except AuthLost:
        raise
    except QobuzError:
        return album, []
    tracks = ((full or {}).get("tracks") or {}).get("items") or []
    return (full or album), tracks


@dataclass
class DirMatch:
    """How one owned folder matched the catalog. status is one of: partial,
    complete, false_match, predicted_path_mismatch, no_tracks, no_match."""
    status: str
    album_dir: Path
    qobuz_album: dict | None = None
    missing: list = field(default_factory=list)
    present: list = field(default_factory=list)
    existing: list = field(default_factory=list)


def match_album_dir(album_dir, artist_name, token, *, catalog, prefer_hires):
    """Match one owned folder to the Qobuz edition it actually is and compare
    track-for-track. The single per-folder decision both the CLI gap-fill loop
    and the engine's owned-album pass share, so the two can't drift.

    Resolution is target_dir-aligned (the matched edition resolves back to this
    folder); the redundant predicted-path recheck is kept because a folder that
    fuzz-matches a differently-located album is the classic false match.
    """
    album = find_qobuz_album_for_dir(
        album_dir, artist_name, token, prefer_hires=prefer_hires,
        catalog=catalog, target_dir=album_dir)
    if album is None:
        return DirMatch("no_match", album_dir)
    predicted = find_album_dir_filesystem(album)
    if predicted is None or not _paths_equal(predicted, album_dir):
        return DirMatch("predicted_path_mismatch", album_dir, album)
    tracks = (album.get("tracks") or {}).get("items") or []
    if not tracks:
        return DirMatch("no_tracks", album_dir, album)
    existing, _ = find_existing_tracks(album, album_dir=album_dir)
    missing, present = compute_missing(tracks, existing)
    if existing and not present:
        return DirMatch("false_match", album_dir, album, existing=existing)
    status = "partial" if missing else "complete"
    return DirMatch(status, album_dir, album,
                    list(missing), list(present), existing)


def discover_fully_missing(artist_name, catalog, opts, *, hidden=None,
                           handled_ids=frozenset(), resolved_dirs=frozenset(),
                           owned_titles=None, token=None):
    """Catalog albums the owned-album pass didn't account for: fully-missing
    releases, plus a collaboration filed under another artist's folder that
    still has track gaps. Shared by the CLI missing-albums step and the web
    scan. handled_ids / resolved_dirs / owned_titles come from the owned pass so
    an album already matched to a folder isn't re-offered as missing.
    """
    owned_titles = owned_titles or {}
    gaps = []
    pairs = dedup_album_versions(
        [a for a in catalog if is_lossless_album(a)],
        prefer_hires=opts.prefer_hires)
    if not opts.include_comps:
        pairs = filter_compilation_albums(pairs, artist_name)
    if not opts.include_singles:
        pairs = filter_short_releases(pairs, cfg.MISSING_ALBUMS_MIN_TRACKS)
    for album, _n_versions in pairs:
        if album.get("id") in handled_ids:
            continue
        if _is_hidden(hidden, artist_name, album):
            continue
        existing, album_dir = find_existing_tracks(album)
        if album_dir is not None and str(album_dir) in resolved_dirs:
            continue
        if not existing:
            # The owned pass walks folders directly under the artist; a same-
            # named folder its resolution missed must not be re-offered.
            if _owned_by_name(owned_titles, album):
                continue
            gaps.append(AlbumGap(album, None))
            continue
        # Resolves to a folder the owned pass didn't walk (a collaboration filed
        # under a second artist). Offer only the genuine gap, never a re-download
        # over a folder that merely fuzz-matched (no track overlap).
        album, tracks = _materialize_tracks(album, token)
        if not tracks:
            continue
        missing, present = compute_missing(tracks, existing)
        if missing and present:
            gaps.append(AlbumGap(album, album_dir, list(missing), list(present)))
    return gaps


def find_missing_for_artist(query, *, token, opts=None, artist_dir=None,
                            hidden=None, want_missing=True, skip_dir=None):
    """Find what's missing for one artist.

    query        — artist name (or folder name) used to resolve the Qobuz artist.
    opts         — DiscoveryOpts (prefer_hires / include_comps / include_singles).
    artist_dir   — the artist's on-disk folder; resolved from query when omitted.
    hidden       — a loaded hidden-store (library.hidden.load()) to filter bulk
                   walks; pass None for a single-artist request, which sees all.
    want_missing — when False, only owned-album gaps are returned (no fully-
                   missing albums); the album-fill walk uses this.
    skip_dir     — optional predicate(Path) -> bool; folders it accepts are left
                   unmatched (the album-fill walk skips already-decided albums).
    """
    opts = opts or DiscoveryOpts()
    artist_id, artist_name = resolve_artist(query, token)
    if not artist_id:
        return DiscoveryResult(None, None)
    if artist_dir is None:
        artist_dir = resolve_artist_dir(query)

    catalog, total = get_artist_albums(artist_id, token,
                                       limit=cfg.ARTIST_CATALOG_LIMIT)
    truncated = bool(total and total > len(catalog))
    if truncated:
        log.info(f"  Qobuz lists {total} albums; scanning the first "
                 f"{len(catalog)}.")

    result = DiscoveryResult(artist_id, artist_name, catalog=catalog,
                             catalog_truncated=truncated)
    handled_ids = set()
    resolved_dirs = set()

    album_dirs = list_artist_album_dirs(artist_dir) if artist_dir else []
    owned_titles = owned_album_titles(album_dirs)
    for ad in album_dirs:
        if skip_dir is not None and skip_dir(ad):
            continue
        m = match_album_dir(ad, artist_name, token,
                            catalog=catalog, prefer_hires=opts.prefer_hires)
        classify_owned_match(result, m, hidden, artist_name,
                             handled_ids, resolved_dirs)

    if want_missing:
        result.gaps.extend(discover_fully_missing(
            artist_name, catalog, opts, hidden=hidden, handled_ids=handled_ids,
            resolved_dirs=resolved_dirs, owned_titles=owned_titles, token=token))

    vlog(f"  discovery({artist_name!r}): {len(result.partials)} partial, "
         f"{len(result.fully_missing)} fully-missing, "
         f"{len(result.complete)} complete, {len(result.unmatched_dirs)} unmatched, "
         f"{len(result.skipped)} skipped")
    return result


def classify_owned_match(result, m, hidden, artist_name,
                         handled_ids, resolved_dirs):
    """Fold one DirMatch into the running DiscoveryResult, recording which
    catalog ids and folders the owned pass has now accounted for. A false or
    track-less match still counts as accounted-for (its folder fuzz-resolved
    there), so the missing pass won't re-offer that album; a predicted-path
    mismatch or no-match does not."""
    ad = m.album_dir
    if m.status == "no_match":
        result.unmatched_dirs.append(ad)
        return
    if m.status == "predicted_path_mismatch":
        result.skipped.append({"dir": ad, "reason": "predicted_path_mismatch",
                               "qobuz_title": (m.qobuz_album or {}).get("title") or "?"})
        return
    if m.qobuz_album and m.qobuz_album.get("id") is not None:
        handled_ids.add(m.qobuz_album["id"])
    resolved_dirs.add(str(ad))
    if m.status == "no_tracks":
        result.skipped.append({"dir": ad, "reason": "no_tracks",
                               "qobuz_title": (m.qobuz_album or {}).get("title") or "?"})
        return
    if m.status == "false_match":
        n_qobuz = len((m.qobuz_album.get("tracks") or {}).get("items") or [])
        result.skipped.append({"dir": ad, "reason": "false_match",
                               "qobuz_title": (m.qobuz_album or {}).get("title") or "?",
                               "n_existing": len(m.existing), "n_qobuz": n_qobuz})
        return
    if m.status == "partial":
        if _is_hidden(hidden, artist_name, m.qobuz_album):
            return
        result.gaps.append(AlbumGap(m.qobuz_album, ad, m.missing, m.present))
    else:
        result.complete.append({"dir": ad, "qobuz_album": m.qobuz_album,
                                 "existing": m.existing})
