"""Qobuz search and lookup helpers.

find_qobuz_track_by_isrc is the heart of album repair: it ties
a replacement download to the exact recording the user already had, by
matching on ISRC rather than guessing by album/title/edition.

The equality check is CASE-FOLDED and HYPHEN-STRIPPED but otherwise
STRICT — text-search hits, substrings, and prefixes do NOT satisfy it.
This strictness is load-bearing: a loose match would silently replace
a user's file with the wrong recording.
"""
from qobuz_fetch import config
from qobuz_fetch.api.auth import QobuzError
from qobuz_fetch.api.client import qobuz_get


# ── Album / artist / track search ─────────────────────────────────────────────
def search_albums(query, token, limit=None):
    limit = limit if limit is not None else config.SEARCH_LIMIT
    data = qobuz_get("album/search", {"query": query, "limit": limit}, token)
    return (data.get("albums") or {}).get("items") or []


def search_artists(query, token, limit=10):
    data = qobuz_get("artist/search", {"query": query, "limit": limit}, token)
    return (data.get("artists") or {}).get("items") or []


def get_album(album_id, token):
    return qobuz_get("album/get", {"album_id": album_id, "extra": "track_ids"}, token)


def search_tracks(query, token, limit=10):
    """Generic Qobuz track search. Returns list of track dicts.

    Each track dict carries the standard fields (id, title, duration, isrc,
    track_number, media_number, version) plus an embedded `album` dict
    with id/title/artist/etc. Used by find_qobuz_track_by_isrc to resolve
    on-disk files to exact-recording replacements without album-level
    edition guessing.
    """
    data = qobuz_get("track/search", {"query": query, "limit": limit}, token)
    return (data.get("tracks") or {}).get("items") or []


# ── ISRC lookup — STRICT equality (replacing the wrong recording is silent data loss) ────
def find_qobuz_track_by_isrc(isrc, token):
    """Look up a Qobuz track by exact ISRC. Returns the track dict or None.

    Qobuz indexes ISRCs in track/search, but a text query for an ISRC
    string can also surface tracks whose ISRC merely starts with the same
    characters, or tracks whose title coincidentally contains the string.
    This wrapper requires the returned track's ISRC field to equal the
    query (case-folded, hyphens stripped) before declaring a hit, so
    "exact match" actually means it.

    Returns None for: empty input, API error, no hit, or only soft hits.
    Caller decides what to do with None (skip, fall back to title match,
    or prompt the user).
    """
    if not isrc:
        return None
    isrc_n = str(isrc).replace("-", "").upper().strip()
    if not isrc_n:
        return None
    try:
        results = search_tracks(isrc_n, token, limit=25)
    except QobuzError:
        return None
    for t in results:
        t_isrc = (t.get("isrc") or "").replace("-", "").upper().strip()
        if t_isrc and t_isrc == isrc_n:
            return t
    return None


# ── Artist discography (paginated) ────────────────────────────────────────────
def get_artist_albums(artist_id, token, limit=None):
    """Return (items, qobuz_total) for an artist's full discography.
    Items are search-shaped (no tracks); call get_album() for those."""
    limit = limit if limit is not None else config.ARTIST_CATALOG_LIMIT
    items = []
    offset = 0
    qobuz_total = None
    page_size = config.ARTIST_CATALOG_PAGE
    while len(items) < limit:
        want = min(page_size, limit - len(items))
        data = qobuz_get("artist/get", {
            "artist_id": artist_id,
            "extra": "albums",
            "limit": want,
            "offset": offset,
        }, token)
        albums_obj = data.get("albums") or {}
        block = albums_obj.get("items") or []
        if qobuz_total is None:
            t = albums_obj.get("total")
            if isinstance(t, int):
                qobuz_total = t
        if not block:
            break
        items.extend(block)
        offset += len(block)
        # Short page = end of data on Qobuz's side; stop early.
        if len(block) < want:
            break
        if qobuz_total is not None and len(items) >= qobuz_total:
            break
    return items, qobuz_total
