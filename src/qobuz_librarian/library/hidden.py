"""Albums the user dismissed from the bulk library/upgrade walks.

The walks resurface every gap on every run. With a large library triaged over
weeks, that means re-reviewing the same albums you've already decided you don't
want, forever. This module is the memory that stops it: a dismissed album is
recorded by a loose artist+title fingerprint and filtered out of the bulk walks
until the user restores it.

Keyed on a fingerprint, not the Qobuz album id: dedup can resolve to a
different edition (a new id) of the same album on a later scan, and an id key
would let that edition slip back onto the list. The fingerprint keeps every
edition of one album dismissed together. The year is kept for the Hidden view
but is deliberately not part of the match key — a remaster carries a different
year, and a missing year (common from Qobuz) would otherwise mis-key.

Hides are scoped so the two walks don't cross-contaminate: a "missing" hide
(don't offer to download this album I don't own) is independent of an "upgrade"
hide (don't offer to re-rip this album I own at higher quality). Restoring one
scope never touches the other.

The single-artist Artist page does NOT consult this store — typing a name is a
conscious request to see everything by that artist. Only the bulk walks filter
on it.
"""
import json
from datetime import datetime, timezone

from qobuz_librarian import config as cfg
from qobuz_librarian.library.tags import normalize, strip_album_decorations

SCOPE_MISSING = "missing"
SCOPE_UPGRADE = "upgrade"
_SCOPES = (SCOPE_MISSING, SCOPE_UPGRADE)


def album_fingerprint(artist, title):
    """Loose key tying every edition of one album together.

    Returns None when artist or title normalize to nothing (pure-CJK / emoji
    titles) — the caller treats that as 'can't fingerprint', so such an album
    is never matched against the store and stays visible rather than being
    wrongly hidden.
    """
    a = normalize(artist or "")
    t = normalize(strip_album_decorations(title or ""))
    if not a or not t:
        return None
    return f"{a}|{t}"


def load():
    """Return the whole store as {scope: {fingerprint: entry}}, tolerating a
    missing or corrupt file by returning empty scopes."""
    base = {s: {} for s in _SCOPES}
    if not cfg.HIDDEN_FILE.exists():
        return base
    try:
        with open(cfg.HIDDEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(data, dict):
        return base
    for scope in _SCOPES:
        bucket = data.get(scope)
        if isinstance(bucket, dict):
            base[scope] = {k: v for k, v in bucket.items() if isinstance(v, dict)}
    return base


def save(store):
    try:
        cfg.HIDDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.HIDDEN_FILE.with_suffix(cfg.HIDDEN_FILE.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2, ensure_ascii=False)
        tmp.replace(cfg.HIDDEN_FILE)
    except OSError:
        pass


def is_hidden(scope, artist, title, store):
    """True when (artist, title) is dismissed in this scope. `store` is a
    preloaded dict from load() so a scan doesn't re-read the file per album."""
    fp = album_fingerprint(artist, title)
    if fp is None:
        return False
    return fp in (store.get(scope) or {})


def hide(scope, items):
    """Record dismissals. `items` is an iterable of (artist, title, year).
    Returns the number newly hidden; an already-hidden album is a no-op."""
    store = load()
    bucket = store.setdefault(scope, {})
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for artist, title, year in items:
        fp = album_fingerprint(artist, title)
        if fp is None or fp in bucket:
            continue
        bucket[fp] = {"artist": artist or "", "title": title or "",
                      "year": str(year or ""), "ts": now}
        added += 1
    if added:
        save(store)
    return added


def restore(scope, artists):
    """Un-hide every album whose stored artist is in `artists`. Returns the
    count removed."""
    store = load()
    bucket = store.get(scope) or {}
    targets = set(artists)
    drop = [fp for fp, e in bucket.items() if (e.get("artist") or "") in targets]
    for fp in drop:
        bucket.pop(fp, None)
    if drop:
        store[scope] = bucket
        save(store)
    return len(drop)


def hidden_by_artist(scope, store=None):
    """[{artist, albums: [{title, year, ts}]}], sorted for the Hidden view."""
    bucket = (store if store is not None else load()).get(scope) or {}
    groups = {}
    for e in bucket.values():
        artist = e.get("artist") or "Unknown artist"
        groups.setdefault(artist, []).append({
            "title": e.get("title") or "?",
            "year": e.get("year") or "",
            "ts": e.get("ts") or "",
        })
    out = []
    for artist in sorted(groups, key=str.lower):
        albums = sorted(groups[artist], key=lambda a: (a["title"].lower(), a["year"]))
        out.append({"artist": artist, "albums": albums})
    return out


def count(scope, store=None):
    return len((store if store is not None else load()).get(scope) or {})
