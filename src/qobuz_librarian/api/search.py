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
def _expect_dict(data, endpoint):
    """qobuz_get returns r.json() untyped, so a malformed/error 200 body
    (a JSON list/str/number — e.g. a CDN/proxy error page served with 200) would
    crash on the `.get` below and escape callers that only catch QobuzError.
    Turn it into the QobuzError they already handle, matching get_album."""
    if not isinstance(data, dict):
        raise QobuzError(f"{endpoint} returned a non-dict response")
    return data


def search_albums(query, token, limit=None):
    limit = limit if limit is not None else config.SEARCH_LIMIT
    data = _expect_dict(
        qobuz_get("album/search", {"query": query, "limit": limit}, token),
        "album/search")
    items = (data.get("albums") or {}).get("items") or []
    for a in items:
        _normalize_album_fields(a)
    return items


def search_artists(query, token, limit=10):
    data = _expect_dict(
        qobuz_get("artist/search", {"query": query, "limit": limit}, token),
        "artist/search")
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
    if not isinstance(album, dict):
        # A malformed/error 200 body (list/str/None) would crash on `.get` below
        # and escape every caller that only catches QobuzError — raise one here.
        raise QobuzError(f"album/get returned a non-dict response for {album_id!r}")
    # Don't cache a track-less response. The album cache has no TTL, so a
    # transient/partial 200 with an empty tracks block would otherwise report
    # the album as empty forever; serve it this once but only persist a real
    # track list.
    if (album.get("tracks") or {}).get("items"):
        album_cache.put(album_id, album)
    return album


def get_track(track_id, token):
    """Fetch one Qobuz track by id (for a pasted track URL in Tracks mode).

    Returns the track dict — which carries its `album` sub-object, so the search
    results renderer has everything it needs — or None when the id doesn't
    resolve to a real track."""
    t = qobuz_get("track/get", {"track_id": track_id}, token)
    return t if isinstance(t, dict) and t.get("id") else None


def search_tracks(query, token, limit=10):
    """Generic Qobuz track search. Returns list of track dicts.

    Each track dict carries the standard fields (id, title, duration, isrc,
    track_number, media_number, version) plus an embedded `album` dict
    with id/title/artist/etc. Used by find_qobuz_track_by_isrc to resolve
    on-disk files to exact-recording replacements without album-level
    edition guessing.
    """
    data = _expect_dict(
        qobuz_get("track/search", {"query": query, "limit": limit}, token),
        "track/search")
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

    Returns None for: empty input, a definitive API error (QobuzError — a 4xx
    or an unparseable body, read as "nothing usable here"), no hit, or only
    near-misses. AuthLost and QobuzUnavailable are NOT swallowed: both are abort
    signals (not QobuzError) and propagate, so an expired token or a Qobuz outage
    stops the run cleanly instead of mislabelling every track as "no ISRC match".
    Caller decides what to do with None (skip, fall back to title match, or
    prompt the user).
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
def get_artist_albums(artist_id, token, limit=None, fresh=False):
    """Return (items, qobuz_total) for an artist's full discography.
    Items are search-shaped (no tracks); call get_album() for those.

    fresh=True skips the catalog cache read and fetches from Qobuz — the
    new-release check needs current data, and a complete fetch refreshes the
    cache as a side effect so later gap scans stay fast. A short or partial
    result (fewer than Qobuz's own total) is used for this run but NOT cached,
    so the next scan re-fetches rather than trust it."""
    limit = limit if limit is not None else config.ARTIST_CATALOG_LIMIT
    from qobuz_librarian.api import album_cache
    cache_key = f"{artist_id}:{limit}"
    if not fresh:
        cached = album_cache.get_catalog(cache_key, config.ARTIST_CATALOG_CACHE_TTL)
        if cached is not None:
            return cached.get("items") or [], cached.get("total")
    items = []
    offset = 0
    qobuz_total = None
    page_size = config.ARTIST_CATALOG_PAGE
    while len(items) < limit:
        want = min(page_size, limit - len(items))
        data = _expect_dict(qobuz_get("artist/get", {
            "artist_id": artist_id,
            "extra": "albums",
            "limit": want,
            "offset": offset,
        }, token), "artist/get")
        albums_obj = data.get("albums") or {}
        block = albums_obj.get("items") or []
        if qobuz_total is None:
            t = albums_obj.get("total")
            if isinstance(t, bool):
                t = None
            elif isinstance(t, str) and t.strip().isdigit():
                t = int(t)
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
    # Only persist a discography we believe is whole. The catalog cache lives
    # for weeks, so an entry that fell short because Qobuz handed back a short
    # or empty page mid-pagination would keep hiding that artist's missing and
    # new albums until it expired. If we got fewer than Qobuz's own total
    # without hitting our own limit, treat the result as incomplete: it's fine
    # for this run, but the next scan should re-fetch rather than trust it.
    complete = (qobuz_total is None
                or len(items) >= qobuz_total
                or len(items) >= limit)
    # Never cache an empty discography unless Qobuz explicitly said the artist
    # has none (total == 0). A 200 with an error body (no "albums" key) yields
    # items=[] and total=None, which would otherwise read as "complete" and
    # hide that artist's whole catalog from scans for the cache's lifetime.
    if complete and (items or qobuz_total == 0):
        album_cache.put_catalog(cache_key, {"items": items, "total": qobuz_total})
    return items, qobuz_total
