"""Library migration — reorganize a messy collection into this tool's layout.

The job is to take an arbitrary, possibly-untagged music folder and produce the
two-level ``<Artist>/<Album> (<Year>)/`` tree the scanner expects, matching the
exact folder shape the downloader itself writes (see ``docker/beets-default.yaml``:
``$albumartist/$album ($year)/%if{$multidisc,Disc $disc/}$track - $title``).

Three rules drive every decision here:

* **Copy, never move.** The default builds the organized library as a copy at a
  separate destination; the source is read but never altered or deleted.
  In-place mode is a separate, explicit opt-in and even then only relocates a
  file once a verified copy exists.
* **Decide, then act.** ``build_plan`` produces the entire set of
  source→destination decisions up front (a pure function over extracted
  metadata). The preview renders that plan; ``execute_plan`` carries out exactly
  that plan. The preview is the truth, not an estimate.
* **When unsure, leave it.** A file whose tags can't place it, an ambiguous
  fingerprint, or a destination collision is left untouched and reported — never
  guessed into the wrong folder.

Tags are the primary source of truth. Fingerprinting (AcoustID) is an opt-in
second stage that only runs against the files tags couldn't place, so a
mostly-tagged library finishes fast and offline.
"""
import csv
import errno
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

from qobuz_librarian import config
from qobuz_librarian.library.scanner import iter_tree_no_symlinks
from qobuz_librarian.library.tags import (
    VA_NORMALIZED,
    beets_sanitize,
    normalize,
    strip_year_decoration,
)
from qobuz_librarian.ui_cli.logging import vlog

log = logging.getLogger("qobuz_librarian")

# beets' chroma plugin ships this public application key and uses it for every
# AcoustID *lookup*; a personal key is only ever needed to *submit* fingerprints
# back. Identification is therefore keyless — reuse the same key here so the
# migration matches what `beet -c beets-chroma.yaml import` would resolve.
ACOUSTID_API_KEY = "1vOwZtEn"

# AcoustID asks callers to stay under ~3 lookups/second on a shared key.
ACOUSTID_RATE_DELAY = 0.34
# A fingerprint match below this confidence, or one that disagrees with another
# high-scoring match, is treated as "couldn't identify" rather than guessed.
ACOUSTID_MIN_SCORE = 0.9

PLACE = "place"
UNPLACEABLE = "unplaceable"
COLLISION = "collision"

COPIED = "copied"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class PlanEntry:
    source: Path
    status: str                       # PLACE | UNPLACEABLE | COLLISION
    dest_rel: Optional[Path] = None   # relative to the destination root
    source_of_truth: str = ""         # "tags" | "acoustid"
    reason: str = ""
    meta: Optional[dict] = None


@dataclass
class MigrationPlan:
    dest_root: Path
    entries: list = field(default_factory=list)

    @property
    def placed(self) -> list:
        return [e for e in self.entries if e.status == PLACE]

    @property
    def unplaceable(self) -> list:
        return [e for e in self.entries if e.status == UNPLACEABLE]

    @property
    def collisions(self) -> list:
        return [e for e in self.entries if e.status == COLLISION]

    def summary(self) -> dict:
        return {
            "total": len(self.entries),
            "place": len(self.placed),
            "unplaceable": len(self.unplaceable),
            "collision": len(self.collisions),
        }


@dataclass
class ExecResult:
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    # In-place moves that reached the destination but whose source couldn't be
    # deleted, so the original still occupies space — counted apart from clean
    # moves so the summary doesn't claim them as fully relocated.
    lingered: int = 0
    # Non-audio companion files (cover art, .cue/.log/.lrc/…) carried alongside
    # the audio into the destination so a migrated album keeps its artwork.
    companions: int = 0
    cancelled: bool = False
    # One (source, dest_rel, status, reason) per attempted file — the record of
    # what actually happened, surfaced to the user and written to the results
    # manifest. status is COPIED | SKIPPED | FAILED.
    outcomes: list = field(default_factory=list)

    @property
    def failures(self) -> list:
        return [(src, reason) for src, _, status, reason in self.outcomes
                if status == FAILED]


# ── Tag extraction ──────────────────────────────────────────────────────────

def _first(tags: Mapping, *keys) -> str:
    """First non-empty value across the given keys, case-insensitively.

    Tag backends return either scalars or single-element lists; both collapse
    to a stripped string here.
    """
    lower = {str(k).lower(): v for k, v in tags.items()}
    for k in keys:
        v = lower.get(k.lower())
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        v = str(v).strip() if v is not None else ""
        if v:
            return v
    return ""


def _num_and_total(s: str):
    """Parse a "3", "03", or "3/12" tag into (number, total). Either may be 0."""
    if not s:
        return 0, 0
    m = re.match(r"^\s*(\d+)\s*(?:/\s*(\d+))?", str(s))
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2) or 0)


def _parse_year(s: str) -> int:
    m = re.search(r"(\d{4})", str(s or ""))
    if not m:
        return 0
    y = int(m.group(1))
    return y if 1000 <= y <= 9999 else 0


def _is_compilation(tags: Mapping, albumartist: str) -> bool:
    flag = _first(tags, "compilation", "cpil", "tcmp").lower()
    if flag in ("1", "yes", "true"):
        return True
    return normalize(albumartist) in VA_NORMALIZED


def normalize_tags(tags: Mapping, stem: str, ext: str) -> dict:
    """Map a raw tag mapping to the fields placement needs.

    Pure: takes an already-read mapping (so it's testable without a real audio
    file). ``stem`` is the source filename minus extension, used as the title
    fallback when no title tag is present.
    """
    albumartist = _first(tags, "albumartist", "album artist", "artist")
    track, _ = _num_and_total(_first(tags, "tracknumber", "track"))
    disc, disc_total_inline = _num_and_total(_first(tags, "discnumber", "disc"))
    disc_total, _ = _num_and_total(_first(tags, "disctotal", "totaldiscs"))
    return {
        "albumartist": albumartist,
        "album": _first(tags, "album"),
        "title": _first(tags, "title") or stem,
        "track": track,
        "disc": disc or 1,
        "disctotal": disc_total or disc_total_inline or 0,
        "year": _parse_year(_first(tags, "originaldate", "date", "year")),
        "compilation": _is_compilation(tags, albumartist),
        "ext": ext.lower(),
    }


def extract_metadata(path: Path) -> Optional[dict]:
    """Read tags from any supported audio file via mutagen; None if unreadable.

    ``easy=True`` gives MP3/MP4 the same friendly key names FLAC/OGG already use,
    so ``normalize_tags`` sees one vocabulary regardless of container.
    """
    try:
        import mutagen
    except ImportError:
        return None
    try:
        f = mutagen.File(str(path), easy=True)
    except Exception:
        return None
    tags = dict(f.tags) if (f is not None and f.tags) else {}
    return normalize_tags(tags, path.stem, path.suffix)


# ── Placement ────────────────────────────────────────────────────────────────

def is_placeable(meta: Optional[dict]) -> bool:
    """A file can be placed from metadata once it has an album artist and album.

    The title falls back to the source filename, so it isn't required; without an
    artist or album there's no folder to build, so the file is left alone."""
    if not meta:
        return False
    return bool(meta.get("albumartist") and meta.get("album"))


_NAME_MAX = 255  # bytes — the per-component limit on ext4/APFS/NTFS and POSIX


def _truncate_component(name: str, *, ext: str = "", limit: int = _NAME_MAX) -> str:
    """Cap a single path component at the filesystem's per-name BYTE limit (beets
    caps at the same 255 by default). A tag long enough to exceed NAME_MAX would
    otherwise raise ENAMETOOLONG from the first Path.exists()/copy and abort the
    whole migration before any plan is built. Truncates on a UTF-8 char boundary
    and reserves room for the extension."""
    ext_b = ext.encode("utf-8", "surrogateescape")
    budget = max(1, limit - len(ext_b))
    b = name.encode("utf-8", "surrogateescape")
    if len(b) <= budget:
        return name + ext
    # Drop a partial trailing multi-byte char rather than splitting it.
    return b[:budget].decode("utf-8", "ignore") + ext


def _dest_exists_or_unstattable(path: Path) -> bool:
    """True if the destination exists OR can't be stat'd. A bare Path.exists()
    re-raises ENAMETOOLONG/EACCES on a pathological path, which would abort the
    whole plan build; treat an unstat-able destination as a collision so that one
    entry is skipped (status COLLISION) rather than crashing the migration."""
    try:
        return path.exists()
    except OSError:
        return True


def album_components(meta: dict) -> tuple:
    """(artist_folder, album_folder) for an album, sanitized to match beets.

    Compilations route under ``Various Artists``; the year is appended only when
    known, so ``Album ()`` never appears."""
    if meta.get("compilation"):
        artist = "Various Artists"
    else:
        artist = meta["albumartist"]
    year = meta.get("year") or 0
    album = meta["album"]
    if year:
        # Strip any year the album tag already carries (messy libraries bake it
        # into the name) so we don't end up with "Album (2010) (2010)".
        album_folder = f"{strip_year_decoration(album)} ({year})"
    else:
        album_folder = album
    return (_truncate_component(beets_sanitize(artist)),
            _truncate_component(beets_sanitize(album_folder)))


def track_filename(meta: dict) -> str:
    track = meta.get("track") or 0
    title = meta.get("title") or ""
    name = f"{track:02d} - {title}" if track > 0 else title
    return _truncate_component(beets_sanitize(name), ext=meta.get("ext", ""))


def _album_key(meta: dict) -> tuple:
    return album_components(meta)


def build_plan(items, dest_root: Path) -> MigrationPlan:
    """Turn ``(source, meta, source_of_truth)`` tuples into a full plan.

    Pure over its inputs (it stats only the destination, to catch a file that
    already exists there). Files with unusable metadata are marked
    ``UNPLACEABLE``; two distinct sources resolving to one destination — or a
    destination that already exists on disk — are all marked ``COLLISION`` and
    skipped. Multi-disc layout is inferred per album from the disc numbers seen.
    """
    dest_root = Path(dest_root)
    placeable, entries = [], []

    for source, meta, sot in items:
        source = Path(source)
        if not is_placeable(meta):
            entries.append(PlanEntry(
                source=source, status=UNPLACEABLE, source_of_truth=sot,
                reason="no usable artist/album tags", meta=meta))
            continue
        placeable.append((source, meta, sot))

    # Group by album so multi-disc can be decided from the whole album, not one
    # track: a disc tag of 2 only means "Disc 2/" if the album really spans
    # discs (disctotal > 1, or some track carries a higher disc number).
    groups: dict = {}
    for source, meta, sot in placeable:
        groups.setdefault(_album_key(meta), []).append((source, meta, sot))

    seen_dest: dict = {}
    built = []
    for key, members in groups.items():
        artist_folder, album_folder = key
        multidisc = any(
            (m.get("disctotal") or 0) > 1 or (m.get("disc") or 1) > 1
            for _, m, _ in members)
        for source, meta, sot in members:
            rel = Path(artist_folder) / album_folder
            if multidisc:
                rel = rel / f"Disc {meta.get('disc') or 1}"
            rel = rel / track_filename(meta)
            built.append((source, sot, meta, rel))
            seen_dest.setdefault(rel, []).append(source)

    for source, sot, meta, rel in built:
        if len(seen_dest[rel]) > 1:
            entries.append(PlanEntry(
                source=source, status=COLLISION, source_of_truth=sot,
                reason="two files map to the same destination", meta=meta))
        elif _dest_exists_or_unstattable(dest_root / rel):
            entries.append(PlanEntry(
                source=source, status=COLLISION, source_of_truth=sot,
                reason="destination already exists", meta=meta))
        else:
            entries.append(PlanEntry(
                source=source, status=PLACE, dest_rel=rel,
                source_of_truth=sot, reason="", meta=meta))

    return MigrationPlan(dest_root=dest_root, entries=entries)


# ── AcoustID second stage (opt-in, container-only) ───────────────────────────

def choose_acoustid_match(candidates, min_score: float = ACOUSTID_MIN_SCORE):
    """Pick a single confident match from AcoustID candidates, or None.

    Pure decision logic, kept apart from the network call so it can be tested
    without a fingerprinter. Returns None when the best score is below
    ``min_score`` (too weak) or when two strong candidates name different
    artists (ambiguous) — both cases mean "leave it and report"."""
    strong = [c for c in candidates if (c.get("score") or 0) >= min_score]
    if not strong:
        return None
    strong.sort(key=lambda c: -(c.get("score") or 0))
    best = strong[0]
    artists = {normalize(c.get("albumartist") or c.get("artist") or "")
               for c in strong}
    artists.discard("")
    if len(artists) > 1:
        return None
    return best


def _first_name(artists) -> str:
    if isinstance(artists, list) and artists:
        return artists[0].get("name") or ""
    return ""


def _pick_album(recording: dict) -> tuple:
    """(album, year, albumartist, compilation) from a matched recording's
    release groups, or ("", 0, "", False) if it lists none.

    Prefers a primary-type "Album" over singles/EPs, takes the earliest release
    year, and flags it a compilation when the release group says so or the album
    artist is a Various-Artists alias."""
    groups = recording.get("releasegroups") or []
    if not groups:
        return "", 0, "", False
    groups = sorted(groups, key=lambda g: 0 if (g.get("type") or "").lower() == "album" else 1)
    rg = groups[0]
    album = rg.get("title") or ""
    albumartist = _first_name(rg.get("artists"))
    secondary = [s.lower() for s in (rg.get("secondarytypes") or [])]
    compilation = ((rg.get("type") or "").lower() == "compilation"
                   or "compilation" in secondary
                   or normalize(albumartist) in VA_NORMALIZED)
    year = 0
    for rel in (rg.get("releases") or []):
        y = (rel.get("date") or {}).get("year") or 0
        if y and (year == 0 or y < year):
            year = y
    return album, year, albumartist, compilation


def identify_from_lookup(resp: dict, min_score: float, stem: str,
                         ext: str) -> Optional[dict]:
    """Turn a raw AcoustID lookup response into placement metadata, or None.

    Pure (no network), so the confidence/ambiguity/album-selection logic is
    testable without a fingerprinter. Returns None when no result clears
    ``min_score`` or is ambiguous, and also when a confident recording lists no
    album — identifying a recording without a release still gives no folder to
    build, so the file stays unplaceable rather than guessed at."""
    candidates = []
    for r in (resp.get("results") or []):
        recs = r.get("recordings") or []
        rec = recs[0] if recs else {}
        candidates.append({
            "score": r.get("score") or 0,
            "artist": _first_name(rec.get("artists")),
            "_rec": rec,
        })
    best = choose_acoustid_match(candidates, min_score)
    if not best:
        return None
    rec = best.get("_rec") or {}
    album, year, albumartist, compilation = _pick_album(rec)
    if not album:
        return None
    return {
        "albumartist": albumartist or best.get("artist") or "",
        "album": album,
        "title": rec.get("title") or stem,
        "track": 0, "disc": 1, "disctotal": 0, "year": year,
        "compilation": compilation, "ext": ext.lower(),
    }


def fingerprint_identify(path: Path, min_score: float = ACOUSTID_MIN_SCORE,
                         ext: str = "") -> Optional[dict]:
    """Identify one file by audio fingerprint via AcoustID, resolving an album
    so the file can actually be placed. None if no confident match (or the
    fingerprinter isn't available).

    Lazily imports ``acoustid`` — it and ``fpcalc`` ship only in the container,
    so this whole stage is a no-op on a host without them. The lookup asks for
    release groups + releases so a recording match yields an album and year, not
    just a title."""
    try:
        import acoustid
    except ImportError:
        vlog("acoustid not installed; skipping fingerprint stage")
        return None
    try:
        duration, fp = acoustid.fingerprint_file(str(path))
        resp = acoustid.lookup(ACOUSTID_API_KEY, fp, duration,
                               meta="recordings releasegroups releases")
    except Exception as e:
        vlog(f"fingerprint failed for {path.name}: {e}")
        return None
    return identify_from_lookup(resp, min_score, path.stem, (ext or path.suffix).lower())


# ── Path validation ───────────────────────────────────────────────────────────

def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def validate_paths(src: Path, dest: Path, *,
                   in_place: bool = False) -> Optional[str]:
    """Reason the source/destination pair is unusable, or None if it's fine.

    The destination has to be a separate tree from the source so a copy can't
    recurse into or overwrite itself."""
    if not src.is_dir():
        return f"Source isn't a readable directory: {src}"
    if in_place and not os.access(str(src), os.W_OK):
        return (f"Source is not writable — in-place mode moves files out of "
                f"the source tree and requires write access: {src}")
    if dest.exists() and not dest.is_dir():
        return f"Destination exists but isn't a folder: {dest}"
    if src.resolve() == dest.resolve():
        return "Source and destination are the same folder."
    if _is_within(dest, src):
        return ("Destination is inside the source — that would copy the new "
                "library into itself. Choose a destination outside the source.")
    if _is_within(src, dest):
        return "Source is inside the destination. Choose separate folders."
    # The copy/move writes into the destination tree (created if absent), so a
    # read-only dest must fail here, not partway through with a raw OSError.
    dest_anchor = dest if dest.exists() else _existing_ancestor(dest)
    if dest_anchor is None or not os.access(str(dest_anchor), os.W_OK):
        return f"Destination isn't writable: {dest_anchor or dest}"
    return None


def _existing_ancestor(path: Path) -> Optional[Path]:
    """The nearest path that exists at or above ``path`` (where a copy lands)."""
    p = Path(path)
    while True:
        if p.exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


def space_estimate(plan: MigrationPlan, *, in_place: bool = False) -> tuple:
    """``(bytes_to_write, free_bytes_at_dest)`` for carrying out ``plan``.

    Only files that actually get written count toward ``bytes_to_write``: every
    placed file in copy mode, but in in-place mode only those on a different
    filesystem than the destination, since a same-filesystem move is a rename
    that consumes no space. ``free_bytes`` is the space available where the new
    library is built, or None when it can't be read (no existing ancestor, or a
    stat error)."""
    anchor = _existing_ancestor(plan.dest_root)
    free = dest_dev = None
    if anchor is not None:
        try:
            free = shutil.disk_usage(anchor).free
            dest_dev = anchor.stat().st_dev
        except OSError:
            free = None
    need = 0
    for entry in plan.placed:
        try:
            st = entry.source.stat()
        except OSError:
            continue
        if in_place and dest_dev is not None and st.st_dev == dest_dev:
            continue
        need += st.st_size
    return need, free


def prune_empty_dirs(root: Path) -> int:
    """Remove now-empty directories beneath ``root`` (never ``root`` itself).

    Run after an in-place migration to clear the husk of the old layout left
    behind once a folder's files have moved out. Only truly empty directories
    go — one still holding any file (cover art, a log, a stray track) is left
    alone. Bottom-up, so a parent that empties out as its children are removed
    is cleared in the same pass; symlinked directories are never followed."""
    root = Path(root)
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False,
                                                  followlinks=False):
        p = Path(dirpath)
        if p == root:
            continue
        try:
            if not any(p.iterdir()):
                p.rmdir()
                removed += 1
        except OSError:
            continue
    return removed


# ── Source collection ─────────────────────────────────────────────────────────

def _audio_files(source_root: Path) -> list:
    exts = set(config.AUDIO_EXTS)
    files = []
    for f in iter_tree_no_symlinks(source_root):
        try:
            if f.is_file() and f.suffix.lower() in exts:
                files.append(f)
        except OSError:
            continue
    files.sort()
    return files


# Non-audio files worth carrying with an album: cover art, embedded booklets,
# rip logs, cue sheets, synced lyrics, and playlists. Only audio gets a plan
# entry, so without an explicit pass these are stranded in the old library.
_COMPANION_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
    ".cue", ".log", ".pdf", ".lrc", ".nfo", ".m3u", ".m3u8",
}


def _carry_companion_files(plan: "MigrationPlan", result: "ExecResult", *,
                           progress: Optional[Callable] = None) -> None:
    """Copy each migrated album folder's non-audio companions into the
    destination folder(s) that received its audio.

    Always a copy (never a move), even in in-place mode: the same cover can fan
    out to several destinations (AcoustID splitting a messy folder), the source
    folder may still hold audio that failed/skipped, and a duplicated cover image
    is harmless. Best-effort — a companion failure is logged, never fatal.
    Same-folder layout only (cover beside the tracks); an album-level cover a
    level above per-disc track folders is intentionally not chased."""
    folder_map: dict = {}
    for source, dest_rel, status, _reason in result.outcomes:
        if status != COPIED or dest_rel is None:
            continue
        folder_map.setdefault(source.parent, set()).add(
            (plan.dest_root / dest_rel).parent)
    for src_folder, dst_folders in folder_map.items():
        try:
            companions = [f for f in src_folder.iterdir()
                          if f.suffix.lower() in _COMPANION_EXTS and f.is_file()]
        except OSError:
            continue
        for dst_folder in dst_folders:
            for f in companions:
                dst = dst_folder / f.name
                # The existence probes are inside the try too: Path.exists()
                # swallows ENOENT but re-raises EACCES/EPERM on a path component,
                # and this whole pass is best-effort — a stray stat error must
                # log-and-skip, never abort the companion sweep for the run.
                try:
                    if not f.exists() or dst.exists():
                        continue
                    if progress:
                        progress("Carrying cover art and sidecars", 0, 0, f.name)
                    _place_file(f, dst, move=False)
                    result.companions += 1
                except (OSError, shutil.Error) as e:
                    log.info(f"  ⚠  couldn't carry {f.name}: {e}")


def collect_items(source_root: Path, *, use_acoustid: bool = False,
                  cancel_check: Optional[Callable[[], bool]] = None,
                  progress: Optional[Callable] = None,
                  min_score: float = ACOUSTID_MIN_SCORE) -> list:
    """Walk the source and return ``(path, meta, source_of_truth)`` tuples.

    Stage 1 reads tags for every file. Stage 2 (only when ``use_acoustid`` is
    set) fingerprints just the files tags couldn't place, so the slow,
    network-bound path never touches the well-tagged majority."""
    import time

    source_root = Path(source_root)
    files = _audio_files(source_root)
    total = len(files)
    items = []
    unplaced = []   # (path, orig_meta) — orig_meta may have track/disc even
                    # when albumartist/album are absent, so keep it for merging.
    for i, f in enumerate(files, 1):
        if cancel_check and cancel_check():
            break
        if progress:
            progress("Reading tags", i, total, f.name)
        meta = extract_metadata(f)
        if is_placeable(meta):
            items.append((f, meta, "tags"))
        else:
            unplaced.append((f, meta))

    if use_acoustid and unplaced:
        n = len(unplaced)
        for i, (f, orig_meta) in enumerate(unplaced, 1):
            if cancel_check and cancel_check():
                items.append((f, None, ""))
                continue
            if progress:
                progress("Fingerprinting unidentified files", i, n, f.name)
            meta = fingerprint_identify(f, min_score=min_score, ext=f.suffix)
            if meta and orig_meta:
                # Prefer the file's own track/disc numbers over AcoustID's
                # zero-defaults: AcoustID identifies the recording but has no
                # reliable per-file position; the embedded tags do.
                if not meta.get("track") and orig_meta.get("track"):
                    meta["track"] = orig_meta["track"]
                if meta.get("disc") == 1 and orig_meta.get("disc", 1) > 1:
                    meta["disc"] = orig_meta["disc"]
                    meta["disctotal"] = orig_meta.get("disctotal") or meta["disctotal"]
            items.append((f, meta, "acoustid" if meta else ""))
            time.sleep(ACOUSTID_RATE_DELAY)
    else:
        items.extend((f, None, "") for f, _ in unplaced)

    return items


# ── Execution ─────────────────────────────────────────────────────────────────

def _same_content(a: Path, b: Path, _chunk: int = 1 << 20) -> bool:
    """True iff a and b are byte-for-byte identical. Streams in chunks (bounded
    memory, short-circuits on first difference). Any OSError reads as 'not
    proven identical' — the caller must then keep the source."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca, cb = fa.read(_chunk), fb.read(_chunk)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


def _place_file(src: Path, dst: Path, *, move: bool) -> None:
    """Materialize ``src`` at ``dst`` safely. Copy by default; move only when
    asked, and only after a verified copy exists.

    Cross-filesystem safe: the bytes land at a ``.partial`` sibling on the
    destination filesystem, are size- AND (for a move) content-verified, then
    atomically renamed into place. An interrupt mid-copy leaves the source intact
    and at worst an orphan ``.partial`` — never a half-written destination."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if move and not src.is_symlink():
        try:
            os.rename(str(src), str(dst))   # atomic same-filesystem move
            return
        except OSError:
            pass                            # cross-fs: fall through to copy
    # A symlinked source skips the rename fast-path: os.rename relocates the LINK
    # itself, leaving the destination pointing at the (possibly out-of-tree)
    # original — a dangle waiting to happen. The copy path below dereferences via
    # shutil.copy2 so the real bytes land at the destination as a real file, then
    # the move branch unlinks the source link.

    tmp = dst.parent / (dst.name + ".partial")
    try:
        shutil.copy2(str(src), str(tmp))
        if tmp.stat().st_size != src.stat().st_size:
            raise OSError("copy size mismatch")
        # A move DELETES the source after this, so a same-length-but-corrupt copy
        # (bit-rot, a driver returning a complete-but-wrong stream, a sparse-hole
        # mismatch) would lose the only good copy. Byte-verify before committing —
        # size alone can't catch it. Copy mode keeps the source, so it can skip
        # the (full-read) content check.
        if move and not _same_content(src, tmp):
            raise OSError("content mismatch after cross-filesystem copy")
        os.replace(str(tmp), str(dst))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    if move:
        # Make the destination durable before deleting the source: a crash in the
        # gap between os.replace and unlink could otherwise leave neither a
        # durable dst nor the source. Best-effort (fsync may be unsupported).
        try:
            dfd = os.open(str(dst.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
        try:
            src.unlink()
        except OSError as e:
            # The destination is verified, so nothing is lost — the source just
            # lingers. Surface it rather than failing the whole entry.
            log.info(f"  ⚠  copied {src.name} but couldn't remove original: {e}")


def execute_plan(plan: MigrationPlan, *, in_place: bool = False,
                 cancel_check: Optional[Callable[[], bool]] = None,
                 progress: Optional[Callable] = None) -> ExecResult:
    """Carry out the placements in ``plan``. Only ``PLACE`` entries are touched;
    unplaceable and colliding files are never read or written here.

    Copy mode never alters the source. A destination that turns up after
    planning is skipped (not overwritten), and a per-file failure is recorded
    and stepped over so one bad file can't abort the run."""
    result = ExecResult()
    placed = plan.placed
    total = len(placed)
    for i, entry in enumerate(placed, 1):
        if cancel_check and cancel_check():
            result.cancelled = True
            break
        src = entry.source
        dst = plan.dest_root / entry.dest_rel
        if progress:
            progress("Copying into the new library", i, total, src.name)
        if dst.exists():
            # Already placed — a re-run after a cancel/crash, or a collision
            # that turned up after planning. Resuming an in-place move where
            # the source is already gone must read as done here, not as a
            # "source vanished" failure (dst is checked first for that reason).
            result.skipped += 1
            result.outcomes.append((src, entry.dest_rel, SKIPPED,
                                    "destination already present"))
            continue
        if not src.exists():
            result.failed += 1
            result.outcomes.append((src, entry.dest_rel, FAILED,
                                    "source vanished before copy"))
            continue
        try:
            _place_file(src, dst, move=in_place)
            if in_place and src.exists():
                # The copy verified but the original couldn't be removed
                # (logged in _place_file). The file is at the destination, but
                # the source still occupies space — report it apart from a
                # clean move so prune/summary don't treat it as relocated.
                result.lingered += 1
                result.outcomes.append((src, entry.dest_rel, COPIED,
                                        "source could not be removed"))
            else:
                result.copied += 1
                result.outcomes.append((src, entry.dest_rel, COPIED, ""))
        except (OSError, shutil.Error) as e:
            result.failed += 1
            result.outcomes.append((src, entry.dest_rel, FAILED, str(e)))
            if getattr(e, "errno", None) == errno.ENOSPC:
                result.cancelled = True
                break
    if not result.cancelled:
        _carry_companion_files(plan, result, progress=progress)
    return result


def write_manifest(plan: MigrationPlan, path: Path) -> None:
    """Write every decision to a CSV audit trail next to the new library.

    One row per file: what happened, what the call was based on, where it went
    (blank when nothing was placed), and why. This is the record that makes the
    result reviewable and the whole migration reversible."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["status", "source_of_truth", "source", "destination", "reason"])
        for e in plan.entries:
            dest = str(e.dest_rel) if e.dest_rel else ""
            w.writerow([e.status, e.source_of_truth, str(e.source), dest, e.reason])


def write_results_manifest(result: ExecResult, path: Path) -> None:
    """Record what the execution actually did — one row per attempted file.

    Sits beside the plan manifest: the plan says where every file *would* go,
    this says where it *went* (copied / skipped / failed) and why, so the run is
    auditable after the fact and not just before it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["status", "source", "destination", "reason"])
        for source, dest_rel, status, reason in result.outcomes:
            w.writerow([status, str(source),
                        str(dest_rel) if dest_rel else "", reason])
