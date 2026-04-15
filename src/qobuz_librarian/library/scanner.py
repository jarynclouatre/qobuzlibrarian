"""Filesystem scanning.

Things worth knowing if you edit this:

- `list_library_artists` and `list_artist_album_dirs` skip dot-folders.
  Without this, hidden dirs like `.Trash` or `.DS_Store/` get treated
  as artists and break later matching.
- `list_library_artists` also excludes `STAGING_DIR` so in-progress
  downloads never get scanned as if they were library content.
- `read_album_dir` walks per-disc subdirs (`CD1/`, `CD2/`) but never
  follows symlinks, so a loop in the library can't recurse forever.
  Reads from module-level `config.AUDIO_EXTS` so a formats-list change
  in config takes effect without touching this file.
- Only `.flac` gets full mutagen metadata; other audio formats get
  filename-only tags so bonus tracks (mp3, m4a from older rips) are
  still visible to `find_extras_in_existing`.
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
    """rglob('*') equivalent that never traverses symlinks.

    `Path.rglob` follows symlinks by default — a loop in MUSIC_ROOT
    sends it into unbounded recursion. Yields symlinked subdirs as
    leaves so the caller still sees them, but never descends.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        for name in dirnames:
            yield dp / name
        for name in filenames:
            yield dp / name

log = logging.getLogger("qobuz_librarian")

try:
    from mutagen.flac import FLAC as MutagenFLAC
    HAVE_MUTAGEN = True
except ImportError:
    MutagenFLAC = None
    HAVE_MUTAGEN = False


# ── Track-number parsing ──────────────────────────────────────────────────────
def parse_track_num(s):
    """Parse a FLAC TRACKNUMBER or DISCNUMBER tag value to int.

    Handles '1', '01', '1/12', '01/12', '5 of 12'. Returns 0 on empty or
    unparseable. (regex form, not split — more
    defensive against exotic tag values).
    """
    if not s:
        return 0
    m = re.match(r"^\s*(\d+)", str(s))
    return int(m.group(1)) if m else 0


# ── FLAC metadata ─────────────────────────────────────────────────────────────
def read_flac_meta(path: Path):
    """Read FLAC tags and audio info via mutagen. Returns dict or None.

    Returns None when mutagen is unavailable or the file can't be read.
    Caller falls back to filename-based tags.
    """
    if not HAVE_MUTAGEN:
        return None
    cached = flac_cache.get(path)
    if cached is not None:
        return cached
    try:
        f = MutagenFLAC(str(path))
    except Exception:
        return None

    def first(key):
        v = f.tags.get(key) if f.tags else None
        if v and isinstance(v, list):
            return v[0]
        return ""

    info = f.info
    meta = {
        "title":       first("TITLE") or first("title"),
        "isrc":        (first("ISRC") or first("isrc") or "").strip().replace("-", "").upper(),
        "mb_trackid":  (first("MUSICBRAINZ_TRACKID") or first("musicbrainz_trackid") or "").strip().lower(),
        "album":       first("ALBUM"),
        "albumartist": first("ALBUMARTIST") or first("ARTIST"),
        "tracknumber": parse_track_num(first("TRACKNUMBER") or first("tracknumber")),
        "discnumber":  parse_track_num(first("DISCNUMBER") or first("discnumber")) or 1,
        "bits":        getattr(info, "bits_per_sample", 0) if info else 0,
        "sample_rate": getattr(info, "sample_rate", 0) if info else 0,
        "channels":    getattr(info, "channels", 0) if info else 0,
        "length":      getattr(info, "length", 0.0) if info else 0.0,
        "path":        str(path),
    }
    flac_cache.put(path, meta)
    return meta


# ── Album directory scan ──────────────────────────────────────────────────────
def read_album_dir(album_dir: Path):
    """Scan album_dir for audio files; return list of track-metadata dicts.

    Only .flac files get full mutagen metadata. Other formats get a
    filename-only fallback so bonus tracks (mp3, m4a, etc.) still appear
    in find_extras_in_existing and aren't silently destroyed by upgrade-replace.

    Uses rglob so tracks in multi-disc subdirectories (CD1/, CD2/) are found.
    Module-level AUDIO_EXTS is used — not a local copy — to stay in sync.
    """
    if not album_dir.exists():
        return []

    audio_files = []
    _exts = set(config.AUDIO_EXTS)
    try:
        for f in iter_tree_no_symlinks(album_dir):
            if f.suffix.lower() in _exts and f.is_file():
                audio_files.append(f)
    except OSError as e:
        vlog(f"walk failed in {album_dir}: {e}")
    audio_files.sort()
    vlog(f"found {len(audio_files)} audio file(s) in {album_dir}")

    tracks = []
    for f in audio_files:
        ext = f.suffix.lower()
        tags = None
        if ext == ".flac" and HAVE_MUTAGEN:
            tags = read_flac_meta(f)
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
                "length":      0.0,
                "path":        str(f),
            }
        tags["normalized"] = normalize(tags["title"])
        try:
            tags["size"] = f.stat().st_size
        except OSError:
            tags["size"] = 0
        tracks.append(tags)
    return tracks


# ── Library directory listing ─────────────────────────────────────────────────
def _has_audio_anywhere(d: Path) -> bool:
    """True if any audio file exists anywhere under ``d`` (recursive).

    Walks without following symlinks and bails on the first hit so the
    cost stays bounded on big trees. Errors during walk count as "no
    audio present" — they'll also break later scans, so flagging the dir
    as empty is the helpful answer.
    """
    exts = set(config.AUDIO_EXTS)
    try:
        for f in iter_tree_no_symlinks(d):
            if f.is_file() and f.suffix.lower() in exts:
                return True
    except OSError:
        return False
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

    Skips hidden dot-folders (.Trash, .DS_Store-style, etc.).
    """
    if not artist_dir.exists():
        return []
    albums = []
    try:
        for d in sorted(artist_dir.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir():
                continue
            if d.name.startswith("."):          # skip hidden dirs (.Trash, .DS_Store/, etc.)
                continue
            albums.append(d)
    except OSError as e:
        vlog(f"list_artist_album_dirs: {e}")
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
    are left alone — deterministic and worth keeping warm."""
    _ARTIST_SUBDIRS_CACHE.clear()
