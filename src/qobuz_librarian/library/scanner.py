"""Filesystem scanning.

Things worth knowing if you edit this:

- `list_library_artists` and `list_artist_album_dirs` skip dot-folders and
  folders with no audio in their tree. Without this, hidden dirs like
  `.Trash` and leftover empty folders get treated as content and break
  later matching.
- `list_library_artists` also excludes `STAGING_DIR` so in-progress
  downloads never get scanned as if they were library content.
- `read_album_dir` walks per-disc subdirs (`CD1/`, `CD2/`) but never
  follows symlinks, so a loop in the library can't recurse forever.
- Every audio format is read with mutagen; a file that won't parse or
  has no tags falls back to a title/track guessed from its filename, so
  untagged bonus tracks (mp3, m4a from older rips) stay visible to
  `find_extras_in_existing`.
"""
import logging
import os
import re
from pathlib import Path

from qobuz_librarian import config
from qobuz_librarian.library import flac_cache
from qobuz_librarian.library.tags import normalize
from qobuz_librarian.ui_cli.logging import vlog


def iter_tree_no_symlinks(root: Path):
    """Yield every entry under root, never descending into symlinked dirs.

    A symlink loop inside MUSIC_ROOT must not send a walk into unbounded
    recursion, and content linked in from outside an album shouldn't be
    scanned as if it lived there. Symlinked subdirs are yielded as leaves so
    the caller still sees them; they're just never followed.
    """
    def _onerror(err):
        # os.walk swallows scandir failures by default — a permission-denied or
        # I/O-failed subdir would then silently drop its tracks from the scan
        # with no signal at all. Surface it (verbose) so vanished files are at
        # least diagnosable.
        vlog(f"scan: couldn't read {getattr(err, 'filename', root)}: {err}")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False,
                                                onerror=_onerror):
        dp = Path(dirpath)
        for name in dirnames:
            yield dp / name
        for name in filenames:
            yield dp / name

log = logging.getLogger("qobuz_librarian")

try:
    import mutagen
    HAVE_MUTAGEN = True
except ImportError:
    mutagen = None
    HAVE_MUTAGEN = False


# ── Track-number parsing ──────────────────────────────────────────────────────
def parse_track_num(s):
    """Parse a FLAC TRACKNUMBER or DISCNUMBER tag value to int.

    Handles '1', '01', '1/12', '01/12', '5 of 12'. Returns 0 on empty or
    unparseable input.
    """
    if not s:
        return 0
    m = re.match(r"^\s*(\d+)", str(s))
    return int(m.group(1)) if m else 0


# ── Audio metadata ────────────────────────────────────────────────────────────
# Cached sentinel for a file mutagen can't parse / that has no title tag, so the
# filename-fallback files (untagged legacy mp3/m4a) aren't fully re-parsed on
# every scan. Self-invalidates with mtime/size like any cache row.
_NEG_META = {"__neg__": True}


def read_audio_meta(path: Path):
    """Read tags and audio info via mutagen. Returns dict or None.

    Works for any format mutagen understands (FLAC, MP3, M4A, …) through its
    uniform "easy" tag interface. Returns None when mutagen is unavailable,
    the file can't be parsed, or it has no title tag — the caller then derives
    title and track number from the filename, so untagged bonus tracks still
    show up.
    """
    if not HAVE_MUTAGEN:
        return None
    cached = flac_cache.get(path)
    if cached is not None:
        # A cached negative result (unparseable / title-less file): return None
        # without re-parsing — these otherwise pay a full mutagen parse on every
        # scan even though they always fall back to the filename.
        return None if cached.get("__neg__") else cached
    # Capture the file signature before parsing so a file edited mid-scan isn't
    # cached with its new mtime but these now-stale tags.
    sig = flac_cache.signature(path)
    try:
        f = mutagen.File(str(path), easy=True)
    except Exception:
        flac_cache.put(path, _NEG_META, sig=sig)
        return None
    if f is None:
        flac_cache.put(path, _NEG_META, sig=sig)
        return None

    tags = f.tags

    def first(key):
        v = tags.get(key) if tags else None
        if v and isinstance(v, list):
            return v[0]
        return ""

    title = first("title")
    if not title:
        flac_cache.put(path, _NEG_META, sig=sig)
        return None

    info = f.info
    meta = {
        "title":       title,
        "isrc":        first("isrc").strip().replace("-", "").upper(),
        "mb_trackid":  first("musicbrainz_trackid").strip().lower(),
        "album":       first("album"),
        "albumartist": first("albumartist") or first("artist"),
        "tracknumber": parse_track_num(first("tracknumber")),
        "discnumber":  parse_track_num(first("discnumber")) or 1,
        "bits":        getattr(info, "bits_per_sample", 0) if info else 0,
        "sample_rate": getattr(info, "sample_rate", 0) if info else 0,
        "channels":    getattr(info, "channels", 0) if info else 0,
        "length":      getattr(info, "length", 0.0) if info else 0.0,
        "path":        str(path),
        # Carry the size from the signature so read_album_dir doesn't have to
        # re-stat every audio file just for its size (a second stat per file
        # adds up on a NAS-backed library). Cache HIT entries pre-dating this
        # key fall back to the stat there.
        "size":        sig[1] if sig else 0,
    }
    flac_cache.put(path, meta, sig=sig)
    return meta


# ── Album directory scan ──────────────────────────────────────────────────────
def read_album_dir(album_dir: Path):
    """Scan album_dir for audio files; return list of track-metadata dicts.

    Tags are read with mutagen for every format (flac, mp3, m4a, …); a file
    that won't parse or carries no tags falls back to title/track from its
    filename, so even untagged bonus tracks appear in find_extras_in_existing
    and aren't silently destroyed by upgrade-replace.
    Multi-disc subdirectories (CD1/, CD2/) are walked; symlinks never followed.
    """
    if not album_dir.exists():
        return []

    audio_files = []
    _exts = set(config.AUDIO_EXTS)
    try:
        for f in iter_tree_no_symlinks(album_dir):
            # is_file() re-raises EACCES/EIO/ESTALE (only ENOENT-class errors are
            # swallowed by pathlib). Catch per entry so one unreadable file drops
            # only itself, not every track after it — a truncated album list
            # would mislead the backup/upgrade gates into deleting a good copy.
            try:
                if f.suffix.lower() in _exts and f.is_file():
                    audio_files.append(f)
            except OSError as e:
                vlog(f"skipping unreadable entry {f} in {album_dir}: {e}")
    except OSError as e:
        vlog(f"walk failed in {album_dir}: {e}")
    audio_files.sort()
    vlog(f"found {len(audio_files)} audio file(s) in {album_dir}")

    tracks = []
    for f in audio_files:
        tags = read_audio_meta(f)
        if tags is None:
            stem = f.stem
            m = re.match(r"^(\d+)\s*-\s*(.+)$", stem)
            # Derive the disc from a "Disc N" / "CD N" parent so two same-titled
            # tracks on different discs don't collapse to one (disc, title) key.
            disc_m = re.match(r"(?:disc|cd)\s*0*(\d+)", f.parent.name, re.IGNORECASE)
            tags = {
                "title":       m.group(2) if m else stem,
                "tracknumber": int(m.group(1)) if m else 0,
                "isrc":        "",
                "mb_trackid":  "",
                "album":       "",
                "albumartist": "",
                "discnumber":  int(disc_m.group(1)) if disc_m else 1,
                "bits":        0,
                "sample_rate": 0,
                "channels":    0,
                "length":      0.0,
                "path":        str(f),
            }
        tags["normalized"] = normalize(tags["title"])
        # read_audio_meta now carries size from the file signature; only the
        # filename-fallback path (above) and pre-existing cache entries that
        # predate the "size" key fall through to a stat here.
        if "size" not in tags:
            try:
                tags["size"] = f.stat().st_size
            except OSError:
                tags["size"] = 0
        tracks.append(tags)
    return tracks


# ── Library directory listing ─────────────────────────────────────────────────
_HAS_AUDIO_CACHE: dict = {}


def _has_audio_anywhere(d: Path) -> bool:
    """True if any audio file exists anywhere under ``d`` (recursive).

    Walks without following symlinks and bails on the first hit so the
    cost stays bounded on big trees. Errors during walk count as "no
    audio present" — they'll also break later scans, so flagging the dir
    as empty is the helpful answer.

    Result cached per path: a single scan calls this once per artist plus
    once per album dir, but artist-walk/upgrade-walk/lyric-walk all hit
    list_artist_album_dirs for the same artists in turn, and the catalog
    fuzzy-resolution fall-through re-asks the same dirs again — a fresh
    iter_tree per call is wasted iterdir+stat on every album subtree.
    """
    key = str(d)
    if key in _HAS_AUDIO_CACHE:
        return _HAS_AUDIO_CACHE[key]
    exts = set(config.AUDIO_EXTS)
    try:
        for f in iter_tree_no_symlinks(d):
            if f.is_file() and f.suffix.lower() in exts:
                _HAS_AUDIO_CACHE[key] = True
                return True
    except OSError:
        # A transient EACCES/EIO/ESTALE walk failure is NOT proof the dir is
        # empty — caching a sticky False here would drop a real artist/album
        # from the whole scan until clear_scan_caches() runs. Answer
        # conservatively for this call, but don't poison the cache: let the
        # next call re-check.
        return False
    _HAS_AUDIO_CACHE[key] = False
    return False


def list_library_artists():
    """List artist directories under MUSIC_ROOT.

    Skips dot-folders (startswith(".")) and the staging
    directory. Sorted by name (case-insensitive). Empty artist directories
    (no audio files anywhere in the tree) are also skipped — they cost an
    API round-trip during scans for zero gain and clutter the walk output.
    A single info line names anything skipped so the user can hand-clean.

    Used for fuzzy resolution and the library / walk+queue / album-fill
    walks.
    """
    if not config.MUSIC_ROOT.exists():
        return []
    artists = []
    empties = []
    try:
        for d in config.MUSIC_ROOT.iterdir():
            if not d.is_dir():
                continue
            if d.name.startswith("."):          # skip hidden dirs (.Trash, .DS_Store/, etc.)
                continue
            if d.resolve() == config.STAGING_DIR.resolve():
                continue
            if not _has_audio_anywhere(d):
                empties.append(d.name)
                continue
            artists.append(d)
    except OSError as e:
        log.info(f"  ⚠  Couldn’t list MUSIC_ROOT: {e}.")
    if empties:
        names = ", ".join(sorted(empties)[:5])
        more = f" (+{len(empties) - 5} more)" if len(empties) > 5 else ""
        log.info(f"  · Skipping {len(empties)} empty artist dir(s): {names}{more}.")
    return sorted(artists, key=lambda p: p.name.lower())


def list_artist_album_dirs(artist_dir: Path):
    """Album subdirectories under an artist dir, sorted by name.

    Skips hidden dot-folders (.Trash, .DS_Store-style, etc.) and folders with
    no audio anywhere in their tree. An empty album folder owns nothing to
    match, upgrade or repair, and resolving one by its name alone only yields a
    confusing "0 present" result; this mirrors list_library_artists, which
    drops empty artist dirs for the same reason. A short notice names anything
    skipped so the user can hand-clean leftover folders.
    """
    if not artist_dir.exists():
        return []
    albums = []
    empties = []
    try:
        for d in sorted(artist_dir.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir():
                continue
            if d.name.startswith("."):          # skip hidden dirs (.Trash, .DS_Store/, etc.)
                continue
            if d.name.endswith(".restore_trash"):  # leftover from an interrupted restore
                continue
            if not _has_audio_anywhere(d):
                empties.append(d.name)
                continue
            albums.append(d)
    except OSError as e:
        vlog(f"list_artist_album_dirs: {e}")
    if empties:
        names = ", ".join(empties[:5])
        more = f" (+{len(empties) - 5} more)" if len(empties) > 5 else ""
        # Verbose-only: in a whole-library sweep this fires per artist and floods
        # the activity log. It's a hand-clean hint, not something every scan needs.
        vlog(f"  · {artist_dir.name}: skipping {len(empties)} empty album "
             f"folder(s): {names}{more}.")
    return albums


# ── Per-scan directory cache ──────────────────────────────────────────────────
# Cleared via clear_scan_caches() at every top-level mode entry so memory
# stays bounded. The fuzzy-match fallback in find_album_dir_filesystem hits
# the same artist dir for every album; the cache turns N iterdir() calls
# per artist into one.
_ARTIST_SUBDIRS_CACHE: dict = {}


def _list_artist_subdirs_cached(artist_dir: Path):
    key = str(artist_dir)
    if key in _ARTIST_SUBDIRS_CACHE:
        return _ARTIST_SUBDIRS_CACHE[key]
    try:
        subdirs = sorted((d for d in artist_dir.iterdir() if d.is_dir()),
                         key=lambda p: p.name.lower())
    except OSError as e:
        vlog(f"  iterdir failed for {artist_dir}: {e}")
        subdirs = []
    _ARTIST_SUBDIRS_CACHE[key] = subdirs
    return subdirs


def clear_scan_caches():
    """Drop per-scan caches. Pure-function lru_caches (normalize / etc.)
    are left alone — deterministic and worth keeping warm.

    Also drains the flac_cache write buffer so anything parsed mid-scan is
    on disk before the next pass starts (the scan-end commit point — put()
    buffers rather than committing per-file to keep a cold 200k-track scan
    out of per-file disk-sync territory)."""
    _ARTIST_SUBDIRS_CACHE.clear()
    _HAS_AUDIO_CACHE.clear()
    flac_cache.flush_pending()


def drop_artist_subdirs_cache(artist_dir):
    """Invalidate the cached subdir listing for one artist, not the whole map.

    Use this when only one artist's library folder has changed on disk (a
    beets rename of just-imported album, an in-place upgrade landing) — a
    bulk-upgrade pass touches one artist at a time, so the full-cache wipe
    `clear_scan_caches()` does would cold-rebuild every OTHER artist's
    listing on the next item too. Quiet on a missing key."""
    if artist_dir is None:
        return
    _ARTIST_SUBDIRS_CACHE.pop(str(artist_dir), None)
