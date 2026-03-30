"""Qobuz search and lookup helpers.

find_qobuz_track_by_isrc is the heart of album repair: it ties
a replacement download to the exact recording the user already had, by
matching on ISRC rather than guessing by album/title/edition.

The equality check is CASE-FOLDED and HYPHEN-STRIPPED but otherwise
STRICT — text-search hits, substrings, and prefixes do NOT satisfy it.
This strictness is load-bearing: a loose match would silently replace
a user's file with the wrong recording.
"""
from qobuz_librarian import config
from qobuz_librarian.api.auth import QobuzError
from qobuz_librarian.api.client import qobuz_get
from qobuz_librarian.library.tags import clean_qobuz_string


def _normalize_album_fields(album):
    """Trim surrounding whitespace and outer quotes from text fields in a
    Qobuz album dict. Mutates and returns the dict.

    Applied at every API boundary that returns albums (search_albums,
    get_album, search_tracks via embedded album, get_artist_albums) so
    downstream code never has to remember to clean again.
    """
    if not isinstance(album, dict):
        return album
    if "title" in album:
        album["title"] = clean_qobuz_string(album.get("title"))
    artist = album.get("artist")
    if isinstance(artist, dict) and "name" in artist:
        artist["name"] = clean_qobuz_string(artist.get("name"))
    tracks_block = album.get("tracks")
    if isinstance(tracks_block, dict):
        items = tracks_block.get("items")
        if isinstance(items, list):
            for t in items:
                _normalize_track_fields(t)
    return album


def _normalize_track_fields(track):
    """Trim surrounding whitespace and outer quotes from a track dict's
    user-visible text fields. Mutates and returns the dict."""
    if not isinstance(track, dict):
        return track
    if "title" in track:
        track["title"] = clean_qobuz_string(track.get("title"))
    if "album_artist" in track:
        track["album_artist"] = clean_qobuz_string(track.get("album_artist"))
    if "version" in track and track.get("version"):
        track["version"] = clean_qobuz_string(track.get("version"))
    inner_album = track.get("album")
    if isinstance(inner_album, dict):
        _normalize_album_fields(inner_album)
    return track


# ── Album / artist / track search ─────────────────────────────────────────────
def search_albums(query, token, limit=None):
    limit = limit if limit is not None else config.SEARCH_LIMIT
    data = qobuz_get("album/search", {"query": query, "limit": limit}, token)
    items = (data.get("albums") or {}).get("items") or []
    for a in items:
        _normalize_album_fields(a)
    return items


def search_artists(query, token, limit=10):
    data = qobuz_get("artist/search", {"query": query, "limit": limit}, token)
    items = (data.get("artists") or {}).get("items") or []
    for a in items:
        if isinstance(a, dict) and "name" in a:
            a["name"] = clean_qobuz_string(a.get("name"))
    return items


def get_album(album_id, token):
    from qobuz_librarian.api import album_cache
    cached = album_cache.get(album_id)
    if cached is not None:
        return cached
    album = qobuz_get("album/get", {"album_id": album_id, "extra": "track_ids"}, token)
    album = _normalize_album_fields(album)
    album_cache.put(album_id, album)
    return album


def search_tracks(query, token, limit=10):
    """Generic Qobuz track search. Returns list of track dicts.

    Each track dict carries the standard fields (id, title, duration, isrc,
    track_number, media_number, version) plus an embedded `album` dict
    with id/title/artist/etc. Used by find_qobuz_track_by_isrc to resolve
    on-disk files to exact-recording replacements without album-level
    edition guessing.
    """
    data = qobuz_get("track/search", {"query": query, "limit": limit}, token)
    items = (data.get("tracks") or {}).get("items") or []
    for t in items:
        _normalize_track_fields(t)
    return items


# ── ISRC lookup — STRICT equality (replacing the wrong recording is silent data loss) ────
def find_qobuz_track_by_isrc(isrc, token):
    """Look up a Qobuz track by exact ISRC. Returns the track dict or None.

    Qobuz indexes ISRCs in track/search, but a text query for an ISRC
    string can also surface tracks whose ISRC merely starts with the same
    characters, or tracks whose title coincidentally contains the string.
    This wrapper requires the returned track's ISRC field to equal the
    query (case-folded, hyphens stripped) before declaring a hit, so
    "exact match" actually means it.

    Returns None for: empty input, a soft/transient API error (QobuzError),
    no hit, or only soft hits. An expired token (AuthLost) is NOT swallowed —
    it propagates so the run aborts cleanly to re-auth rather than mislabelling
    every track as "no ISRC match". Caller decides what to do with None (skip,
    fall back to title match, or prompt the user).
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
    from qobuz_librarian.api import album_cache
    cache_key = f"{artist_id}:{limit}"
    cached = album_cache.get_catalog(cache_key, config.ARTIST_CATALOG_CACHE_TTL)
    if cached is not None:
        return cached.get("items") or [], cached.get("total")
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
        for a in block:
            _normalize_album_fields(a)
        items.extend(block)
        offset += len(block)
        # Short page = end of data on Qobuz's side; stop early.
        if len(block) < want:
            break
        if qobuz_total is not None and len(items) >= qobuz_total:
            break
    album_cache.put_catalog(cache_key, {"items": items, "total": qobuz_total})
    return items, qobuz_total
