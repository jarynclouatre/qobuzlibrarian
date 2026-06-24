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
import fcntl
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from qobuz_librarian import config as cfg
from qobuz_librarian.library.tags import normalize, strip_album_decorations

log = logging.getLogger("qobuz_librarian")

SCOPE_MISSING = "missing"
SCOPE_UPGRADE = "upgrade"
SCOPE_DOWNSAMPLE = "downsample"
# A deliberately-grabbed single track: the album folder is left partial on
# purpose. Unlike the dismissal scopes above this isn't "reviewed and declined"
# but "I only wanted this track", so it has its own writers (mark/unmark) that
# also store the Qobuz album id, and it drives the "collecting" signal — an
# artist you own only singles by is not surfaced in the bulk walks.
SCOPE_SINGLE = "single"
_SCOPES = (SCOPE_MISSING, SCOPE_UPGRADE, SCOPE_DOWNSAMPLE, SCOPE_SINGLE)

# Serialises the load-modify-save in every mutator. mark_single fires on the
# download executor thread while a scan or request may be hiding/restoring on
# another; load()+save() is a read-modify-write that would otherwise drop one
# side's update. In-process thread guard; _store_lock() adds the cross-process
# half.
_LOCK = threading.RLock()


@contextmanager
def _store_lock():
    """Serialise the hidden-store load-modify-save across BOTH threads and OS
    processes.

    The web hide/restore routes deliberately run without the global run-lock (so
    a review row can still be dismissed while a scan holds it, or after the web
    app hands the run-lock to the terminal). During that CLI handoff a separate
    process can be marking single-track graduations at the same time — two OS
    processes the in-process RLock can't serialise. An flock on a sidecar lock
    file does. Best-effort on the file lock: if it can't be opened (read-only or
    an odd mount) we proceed on the in-process lock alone rather than block a
    dismissal."""
    with _LOCK:
        lock_path = cfg.HIDDEN_FILE.parent / (cfg.HIDDEN_FILE.name + ".lock")
        fh = None
        try:
            cfg.HIDDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            fh = open(lock_path, "w", encoding="utf-8")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            if fh is not None:
                fh.close()
                fh = None
        try:
            yield
        finally:
            if fh is not None:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                finally:
                    fh.close()


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


def _preserve_corrupt_store(reason):
    """Move a corrupt hidden-store aside (….corrupt) and warn, instead of letting
    the next save() silently overwrite it. A dismissed-album list is curated over
    weeks; losing it to one bad read with no trace is the failure this prevents.
    Best-effort — a rename that can't happen (read-only volume) still warns."""
    dest = cfg.HIDDEN_FILE.with_name(cfg.HIDDEN_FILE.name + ".corrupt")
    try:
        cfg.HIDDEN_FILE.replace(dest)
        where = f"kept the unreadable copy at {dest.name}"
    except OSError:
        where = "could not move the unreadable copy aside"
    log.warning("hidden-albums store was corrupt (%s); %s. Your dismissed-album "
                "list may have been reset — recover from that file if needed.",
                reason, where)


def load():
    """Return the whole store as {scope: {fingerprint: entry}}, tolerating a
    missing file by returning empty scopes. A corrupt file is moved aside
    (….corrupt) with a warning rather than silently overwritten by the next
    save() — which would destroy the dismissed-album list with no trace."""
    base = {s: {} for s in _SCOPES}
    if not cfg.HIDDEN_FILE.exists():
        return base
    try:
        with open(cfg.HIDDEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        # Present but unreadable (perms / IO). A rename would likely fail too,
        # so surface it rather than pretending the list was empty.
        log.warning("hidden-albums store could not be read (%s); treating it as "
                    "empty for this run.", e)
        return base
    except json.JSONDecodeError as e:
        _preserve_corrupt_store(e)
        return base
    if not isinstance(data, dict):
        _preserve_corrupt_store("top-level value is not a JSON object")
        return base
    for scope in _SCOPES:
        bucket = data.get(scope)
        if isinstance(bucket, dict):
            base[scope] = {k: v for k, v in bucket.items() if isinstance(v, dict)}
    return base


def save(store):
    # Callers hold _store_lock() across load()+save(), so this writer never races
    # a second writer of the same store. The temp file is still unique per write
    # (mkstemp, not a fixed ".tmp" name): a shared temp name let two concurrent
    # writers — a web hide racing a CLI single-marker during a run-lock handoff —
    # clobber each other's partial write and leave an orphan beside the store.
    try:
        cfg.HIDDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(cfg.HIDDEN_FILE.parent),
                                   prefix=cfg.HIDDEN_FILE.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(store, f, indent=2, ensure_ascii=False)
            os.replace(tmp, cfg.HIDDEN_FILE)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except OSError as e:
        # Don't fail completely silent: a dismissal/single-mark that didn't
        # persist (full or read-only data volume) returns a success count but
        # would re-surface next scan — at least leave a verbose trail.
        from qobuz_librarian.ui_cli.logging import vlog
        vlog(f"hidden-store write failed ({e}); the change may not persist")


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
    with _store_lock():
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
    """Un-hide every album whose stored artist matches one in `artists`. Returns
    the count removed.

    Matched on the normalized artist, like the rest of the store keys, so a
    casing or spacing difference between the posted value and the stored string
    still restores it (and every casing variant of one artist clears together)
    rather than stranding an album as un-restorable."""
    with _store_lock():
        store = load()
        bucket = store.get(scope) or {}
        targets = {normalize(a) for a in artists if normalize(a)}
        drop = [fp for fp, e in bucket.items()
                if normalize(e.get("artist") or "") in targets]
        for fp in drop:
            bucket.pop(fp, None)
        if drop:
            store[scope] = bucket
            save(store)
    return len(drop)


def restore_albums(scope, fingerprints):
    """Un-hide specific albums by fingerprint — the same key is_hidden looks up.

    Unknown fingerprints are silently skipped: the Hidden page can render
    against a slightly stale store (another tab just restored the same row, or
    the bucket changed mid-request) and a hard 404 there would be hostile."""
    with _store_lock():
        store = load()
        bucket = store.get(scope) or {}
        drop = [fp for fp in fingerprints if fp in bucket]
        for fp in drop:
            bucket.pop(fp, None)
        if drop:
            store[scope] = bucket
            save(store)
    return len(drop)


def hidden_by_artist(scope, store=None):
    """[{artist, albums: [{title, year, ts, fp}]}], sorted for the Hidden view.

    `fp` is the fingerprint key, exposed so the per-album Restore button on the
    page can target one row directly via restore_albums()."""
    bucket = (store if store is not None else load()).get(scope) or {}
    groups = {}
    for fp, e in bucket.items():
        artist = e.get("artist") or "Unknown artist"
        groups.setdefault(artist, []).append({
            "title": e.get("title") or "?",
            "year": e.get("year") or "",
            "ts": e.get("ts") or "",
            "fp": fp,
        })
    out = []
    for artist in sorted(groups, key=str.lower):
        albums = sorted(groups[artist], key=lambda a: (a["title"].lower(), a["year"]))
        out.append({"artist": artist, "albums": albums})
    return out


def count(scope, store=None):
    return len((store if store is not None else load()).get(scope) or {})


# ── Singles — a deliberately-grabbed track, not a gap ──────────────────────────

def is_single(artist, title, store):
    """True when (artist, title) is marked as a deliberately-grabbed single, so
    its partial folder isn't read as a gap. `store` is preloaded by load()."""
    fp = album_fingerprint(artist, title)
    if fp is None:
        return False
    return fp in (store.get(SCOPE_SINGLE) or {})


def n_singles_for(artist, store):
    """How many of an artist's albums are marked single. An artist is 'collected'
    (and so surfaced by the bulk walks) only when they own more album folders than
    this — i.e. at least one album that isn't just a grabbed single."""
    a = normalize(artist or "")
    if not a:
        return 0
    return sum(1 for e in (store.get(SCOPE_SINGLE) or {}).values()
               if normalize(e.get("artist") or "") == a)


def mark_single(artist, title, year, album_id):
    """Record that the user grabbed a single from this album. Idempotent; keeps
    the original timestamp on a repeat. Returns True if a mark now exists."""
    fp = album_fingerprint(artist, title)
    if fp is None:
        return False
    with _store_lock():
        store = load()
        bucket = store.setdefault(SCOPE_SINGLE, {})
        prev = bucket.get(fp) or {}
        bucket[fp] = {
            "artist": artist or "", "title": title or "", "year": str(year or ""),
            "album_id": str(album_id or "") or prev.get("album_id", ""),
            "ts": prev.get("ts") or datetime.now(timezone.utc).isoformat(),
        }
        save(store)
    return True


def unmark_single(artist, title):
    """Drop the single mark — graduation (the folder is now complete) or the user
    chose to complete the album. Returns True if a mark was removed."""
    fp = album_fingerprint(artist, title)
    if fp is None:
        return False
    with _store_lock():
        store = load()
        bucket = store.get(SCOPE_SINGLE) or {}
        if fp not in bucket:
            return False
        bucket.pop(fp, None)
        store[SCOPE_SINGLE] = bucket
        save(store)
    return True
