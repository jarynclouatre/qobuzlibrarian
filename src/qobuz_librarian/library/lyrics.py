"""Library-wide lyric backfill — fetch lyrics for tracks already on disk.

The manual counterpart to the import-time hook in ``integrations/lyrics.py``:
that hook lyrics each album as it's downloaded, this walks the whole library
and fills in whatever's missing. Both lean on the same provider engine
(``integrations/lyric_fetch``) and the same per-track state file, so a backfill
skips tracks the import hook already lyriced and vice-versa.

Local apart from the provider HTTP — no Qobuz login needed. Honours the
``LYRICS_FORMAT`` / ``LYRICS_PROVIDERS`` settings; ``LYRICS_ENABLED`` only
gates the automatic import-time fetch, so an explicit backfill runs regardless.
"""
from qobuz_librarian import config as cfg
from qobuz_librarian.integrations import lyric_fetch
from qobuz_librarian.library.scanner import (
    list_artist_album_dirs,
    list_library_artists,
)
from qobuz_librarian.ui_cli.logging import log as _default_log
from qobuz_librarian.ui_cli.logging import report_progress, vlog

HAVE_LYRICS = lyric_fetch.AVAILABLE


def iter_library_flacs(*, artist_dirs=None):
    """Yield ``(path, mtime, size)`` for every FLAC under the given artist dirs.

    Walks artists then albums via the same listing the rest of the app uses,
    so the dot-folder, staging-dir and empty-folder skips all apply. The
    stat is taken once here and handed to both the index and fetch passes so
    neither has to re-stat the file.

    ``artist_dirs=None`` walks every artist under MUSIC_ROOT (the whole-library
    backfill). Pass a list to scope to one artist — the per-artist Lyrics tool
    uses this to fill gaps for just one artist without re-walking everything.
    """
    artists = artist_dirs if artist_dirs is not None else list_library_artists()
    for artist_dir in artists:
        for album_dir in list_artist_album_dirs(artist_dir):
            try:
                flacs = sorted(p for p in album_dir.rglob("*")
                               if p.is_file() and p.suffix.lower() == ".flac")
            except OSError as e:
                vlog(f"lyrics walk: couldn't list {album_dir}: {e}")
                continue
            for fp in flacs:
                try:
                    st = fp.stat()
                except OSError:
                    continue
                yield fp, st.st_mtime, st.st_size


def run_library_lyrics(*, dry_run=False, rescan=False, synced_only=False,
                       should_stop=None, log=None, artist_dirs=None):
    """Fetch lyrics across the whole library (or a subset of artists).

    Returns the fetch engine's outcome counts (``wrote-synced``,
    ``already-synced``, ``not-found``, …) with a ``total`` of files considered.
    Re-running is cheap: the state file lets each pass skip tracks already
    resolved, and provider-unavailable tracks are re-tried on the next run.

    ``artist_dirs`` (default None = whole library) scopes the walk to those
    artists only — the per-artist Lyrics tool passes the one resolved dir.
    """
    log = log or _default_log
    items = list(iter_library_flacs(artist_dirs=artist_dirs))
    total = len(items)
    if not total:
        return {"total": 0}
    paths = [p for p, _, _ in items]

    # Seed the state file with a fast, no-network classification of files that
    # already carry lyrics, so the fetch pass only queries providers for the
    # tracks that actually need them — and its progress count reflects real
    # work instead of ticking through thousands of already-lyriced files. A
    # full rescan re-fetches everything anyway, so it skips this.
    if not rescan:
        report_progress("Scanning library lyrics", 0, total, "")
        lyric_fetch.index_existing(
            items, state_path=cfg.LYRIC_FETCH_STATE_FILE, log=log,
            should_stop=should_stop,
            progress_cb=lambda c, t, name: report_progress(
                "Scanning library lyrics", c, t, name),
        )
        if should_stop and should_stop():
            return {"total": total, "stopped": 1}

    counts = lyric_fetch.fetch_for_paths(
        paths,
        providers=cfg.LYRICS_PROVIDERS or None,
        lyrics_format=cfg.LYRICS_FORMAT,
        dry_run=dry_run,
        rescan=rescan,
        synced_only=synced_only,
        state_path=cfg.LYRIC_FETCH_STATE_FILE,
        log=log,
        should_stop=should_stop,
        progress_cb=lambda c, t, name: report_progress("Fetching lyrics", c, t, name),
    )
    result = dict(counts)
    result["total"] = total
    return result
