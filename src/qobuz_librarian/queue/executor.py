"""Queue executor — download, pre-import hooks, beets, backup resolution."""
import errno
import re
import shutil
import time
from datetime import datetime, timezone

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost
from qobuz_librarian.download import run_album_download
from qobuz_librarian.integrations.beets import (
    _consolidate_duplicate_albums,
    beets_import_albums,
    staging_preflight,
)
from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE, downsample_dir
from qobuz_librarian.integrations.lyrics import (
    _record_post_import_lyric_retry,
    _resolve_signatures_to_paths,
    _run_lyric_hook,
    write_post_import_sidecars,
)
from qobuz_librarian.integrations.rip import (
    files_added_since,
    is_cancel_requested,
    snapshot_staging,
)
from qobuz_librarian.library.backup import (
    backup_album_dir,
    pin_unverified_upgrade_backup,
    restore_gap_fill_backup,
    restore_upgrade_backup,
)
from qobuz_librarian.library.catalog import (
    _count_audio_files_in,
    _is_split_album_merge,
    cleanup_duplicate_art,
    find_album_dir_filesystem,
    prompt_and_migrate_multi_artist_folder,
)
from qobuz_librarian.library.scanner import clear_scan_caches
from qobuz_librarian.ui_cli.colors import C, fmt, section, truncate
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.ui_cli.prompts import log_fetch


def _download_for_queue_item(item):
    """Download one queue item, writing n_ok/n_fail/etc back into the item.
    AuthLost and KeyboardInterrupt propagate to the caller; existing=None lets
    run_album_download read the on-disk tracks only if it reaches the
    backup-present-tracks branch."""
    run_album_download(
        album=item["album"],
        missing=item["missing"],
        present=item["present"],
        album_dir=item.get("album_dir"),
        snapshot=item["snapshot_before"],
        quality=item.get("quality"),
        upgrade_only=item["upgrade_only"],
        force_track_by_track=item.get("force_track_by_track", False),
        result=item,
    )


_DISC_FOLDER_RE = re.compile(r"^(?:disc|cd)\s*\d+", re.IGNORECASE)


def _staged_album_dirs(item):
    """Top-level album dirs (under STAGING_DIR) that hold this item's freshly
    downloaded audio. Disc-folder children roll up to the album dir so beets
    sees a multi-disc release as one album, not one album per disc."""
    snap = item.get("snapshot_before") or set()
    audio = [p for p in files_added_since(snap)
             if p.suffix.lower() in cfg.AUDIO_EXTS]
    if not audio:
        return []
    album_dirs = set()
    for p in audio:
        parent = p.parent
        if _DISC_FOLDER_RE.match(parent.name):
            parent = parent.parent
        album_dirs.add(parent)
    return sorted(album_dirs)


def _run_pre_import_hooks_for_dirs(album_dirs, args):
    """Run compress + lyric hooks scoped to ``album_dirs``. Returns the
    aggregated lyric-signature list so the post-import resolver can map them
    to library paths once beets has moved the files. Raises KeyboardInterrupt
    after logging if either hook is interrupted."""
    sigs = []
    if (cfg.DOWNSAMPLE_HIRES_ENABLED and HAVE_DOWNSAMPLE
            and not getattr(args, "no_downsample",
                            getattr(args, "no_compress", False))):
        for d in album_dirs:
            try:
                downsample_dir(d, verbose=True, base_dir=d, log=log.info)
            except KeyboardInterrupt:
                log.info(fmt(C.YELLOW, "  ⚠  downsample hook interrupted"))
                raise
            except Exception as _ce:
                log.info(fmt(C.YELLOW, f"  ⚠  downsample hook failed: {_ce}"))
    for d in album_dirs:
        try:
            lh_result = _run_lyric_hook(d)
        except KeyboardInterrupt:
            log.info(fmt(C.YELLOW, "  ⚠  lyric hook interrupted"))
            raise
        except Exception as _le:
            log.info(fmt(C.YELLOW, f"  ⚠  lyric hook failed: {_le}"))
            lh_result = None
            # Hook crashed; capture signatures of every staged FLAC under this
            # album dir so the post-import resolution still has a shot. Recording
            # staging paths here would go stale once beets moves the files.
            try:
                from qobuz_librarian.integrations.rip import _flac_signature
                for p in sorted(d.rglob("*.flac")):
                    sig = _flac_signature(p)
                    if sig is not None:
                        sigs.append((sig, str(p)))
            except Exception:
                pass
        if isinstance(lh_result, tuple) and len(lh_result) == 2:
            sigs.extend(lh_result[1])
    return sigs


def _pre_import_staging_hooks(args):
    """Whole-staging pre-import hooks (back-compat).

    Used by the single-album process path (``modes/process.py``) where
    staging holds exactly one album at hook time, so scoping to the whole
    staging dir is equivalent to per-album. The queue executor calls
    ``_run_pre_import_hooks_for_dirs`` directly with the per-item album dirs.
    """
    return _run_pre_import_hooks_for_dirs([cfg.STAGING_DIR], args)


def _import_album_with_retry(album_dirs):
    """Run beets import on ``album_dirs`` with up to ``BEETS_MAX_ATTEMPTS``
    attempts, retrying only on idle-timeout (other failures aren't transient).
    Returns True on import success, False on permanent failure."""
    if not album_dirs:
        return True
    max_attempts = max(1, int(cfg.BEETS_MAX_ATTEMPTS))
    for attempt in range(1, max_attempts + 1):
        kind = beets_import_albums(album_dirs)
        if kind == "ok":
            return True
        if kind == "timeout" and attempt < max_attempts:
            log.info(fmt(C.YELLOW,
                f"  ⏳ beets import timed out (attempt {attempt}/{max_attempts}); "
                f"pausing {int(cfg.BEETS_RETRY_PAUSE)}s before retry."))
            time.sleep(cfg.BEETS_RETRY_PAUSE)
            continue
        return False
    return False


def _move_to_beets_retry(album_dirs, label):
    """Move album dirs that failed all beets attempts into
    STAGING_DIR/<BEETS_RETRY_DIR>/<timestamp>-<label>/ so the next batch
    isn't dragged down by them. The user can replay later with
    ``beet import`` of that subtree."""
    if not album_dirs:
        return
    retry_root = cfg.STAGING_DIR / cfg.BEETS_RETRY_DIR
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:60] or "album"
    dest_parent = retry_root / f"{stamp}-{safe_label}"
    try:
        dest_parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  couldn't create {dest_parent}: {e}; leaving album in staging."))
        return
    moved = 0
    for d in album_dirs:
        if not d.exists():
            continue
        try:
            shutil.move(str(d), str(dest_parent / d.name))
            moved += 1
        except OSError as e:
            log.info(fmt(C.YELLOW,
                f"  ⚠  couldn't move {d.name} to {dest_parent}: {e}"))
    if moved:
        log.info(fmt(C.YELLOW,
            f"  ⏭  parked {moved} album dir(s) at "
            f"{dest_parent.relative_to(cfg.STAGING_DIR)}/ "
            f"— will re-import on the next download run."))


def _dir_has_audio(d):
    """True if any audio file remains under d — i.e. beets didn't move it out."""
    try:
        return any(f.is_file() and f.suffix.lower() in cfg.AUDIO_EXTS
                   for f in d.rglob("*"))
    except OSError:
        return True  # can't tell → assume tracks remain, never delete blindly


def _reimport_parked_albums():
    """Re-attempt beets import on albums an earlier batch parked under
    ``STAGING_DIR/<BEETS_RETRY_DIR>/``. A park almost always means a transient
    cause — a DB lock, an idle-timeout, a momentarily-busy disk — so retrying
    the import (never the download; the files are already on disk) at the start
    of the next flush clears the backlog without re-fetching anything. Groups
    that still fail stay parked for the run after. Returns True if anything
    imported, so the caller knows to run the duplicate-album fold."""
    retry_root = cfg.STAGING_DIR / cfg.BEETS_RETRY_DIR
    if not retry_root.is_dir():
        return False
    groups = sorted(d for d in retry_root.iterdir() if d.is_dir())
    any_ok = False
    for group in groups:
        album_dirs = sorted(d for d in group.iterdir() if d.is_dir())
        if not album_dirs:
            try:
                group.rmdir()
            except OSError:
                pass
            continue
        log.info(fmt(C.GRAY,
            f"  ↻  Re-importing parked album(s): {truncate(group.name, 50)}"))
        _import_album_with_retry(album_dirs)
        # Trust the disk, not the return value. The import-success check counts
        # audio before/after, but it can't see inside the retry tree — so a
        # beets run that exits 0 while skipping a parked album (a library
        # duplicate under `duplicate_action: skip`) would look successful even
        # though it moved nothing. Removing only the album dirs whose tracks
        # actually left keeps the skipped ones parked instead of deleting the
        # only copy.
        kept = []
        for d in album_dirs:
            if _dir_has_audio(d):
                kept.append(d)
            else:
                any_ok = True
                shutil.rmtree(d, ignore_errors=True)
        if kept:
            log.info(fmt(C.YELLOW,
                f"  ⏭  {len(kept)} parked album(s) in {truncate(group.name, 50)} "
                f"still hold tracks — left for a later run."))
        else:
            # Every album in the group moved out, so the group holds no tracks —
            # clear it (and any stray non-audio leftover) rather than leaving an
            # empty husk to be rescanned each flush.
            shutil.rmtree(group, ignore_errors=True)
    return any_ok


_RETRYABLE_STOP_RESULTS = {
    "cancelled", "interrupted", "auth_lost", "disk_full",
    "upgrade_aborted_backup_failed",
}


def _queue_item_needs_retry(item):
    """Whether an item should stay queued for a later run.

    Kept: items stopped before they could finish (cancel / interrupt / auth
    loss / disk full / an upgrade whose backup couldn't be taken) and downloads
    that landed nothing — re-running those is safe and can recover. Dropped:
    anything that landed audio. Those files are now in the library, parked under
    .beets_retry/, or a partial a later scan will pick up, so re-downloading
    would only duplicate them."""
    if item.get("result") in _RETRYABLE_STOP_RESULTS:
        return True
    return item.get("n_ok", 0) == 0


def _resolve_queue_item(item, args, imported_globally):
    """Resolve backup and compute result for one completed queue item.

    Handles art cleanup, split-folder merge, sibling deletion, and backup
    restoration. Mutates item["imported"] and item["_resolved_post_dir"].
    Returns a result dict in process_album's shape.
    """
    item["imported"] = imported_globally
    bp = item.get("backup_path")
    album_dir = item["album_dir"]

    # One strict per-album success flag — art cleanup, sibling deletion,
    # backup resolution, and migration must all agree. A partial result
    # (e.g. 5/12 tracks ok) must not delete backups or siblings.
    _item_strict_success = (
        imported_globally
        and item.get("n_ok", 0) > 0
        and item.get("n_fail", 0) == 0
        and item.get("n_lossy", 0) == 0
    )
    if (getattr(args, "migrate_multi_artist", False) and _item_strict_success):
        _migrated = prompt_and_migrate_multi_artist_folder(item["album"], args)
    else:
        _migrated = None
    post_dir = _migrated or find_album_dir_filesystem(item["album"]) or album_dir
    # For brand-new albums (album_dir=None), find_album_dir_filesystem may
    # return None if the cache hasn't refreshed. Clear and retry once.
    if post_dir is None:
        clear_scan_caches()
        post_dir = find_album_dir_filesystem(item["album"])
    album_has_content = (
        post_dir is not None and post_dir.exists()
        and _count_audio_files_in(post_dir) > 0
    )
    # Stash resolved post-import dir so the lyric-retry resolver can
    # use it without redoing the find_album_dir_filesystem dance.
    item["_resolved_post_dir"] = post_dir
    had_any_success = (
        imported_globally and item.get("n_ok", 0) > 0 and album_has_content
    )

    if had_any_success:
        if post_dir:
            cleanup_duplicate_art(post_dir)
        # Split-folder auto-merge: a gap-fill against a folder beets doesn't
        # name canonically (multi-artist, or missing the year) makes it file
        # the new tracks elsewhere, splitting the album. Pull the old tracks
        # into the dir that now holds the fresh ones.
        try:
            split_artist = (item["album"].get("artist") or {}).get("name") or ""
            if _is_split_album_merge(album_dir, post_dir, split_artist):
                from qobuz_librarian.integrations.beets import _merge_split_folder
                n_merged = _merge_split_folder(post_dir, album_dir)
                if n_merged:
                    log.info(fmt(C.GREEN,
                        f"  ✓  Consolidated {n_merged} existing track(s) "
                        f"into {truncate(post_dir.name, 40)}"))
                else:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Split for {truncate(album_dir.name, 40)} "
                        f"but nothing merged (overlap conflicts in {album_dir})."))
        except Exception as _e_sf:
            vlog(f"split-folder merge raised: {_e_sf}")
        # Sibling deletion requires n_fail == 0 AND n_lossy == 0 — a partial
        # download must not wipe the sibling that may hold the missing tracks.
        if item.get("n_fail", 0) == 0 and item.get("n_lossy", 0) == 0:
            for sib_dir in item.get("siblings_to_delete", []):
                if sib_dir.exists():
                    try:
                        shutil.rmtree(sib_dir)
                        log.info(fmt(C.GRAY, f"  🗑  Removed sibling: {sib_dir.name}"))
                    except OSError as _e_sib:
                        log.info(fmt(C.YELLOW,
                            f"  ⚠  Couldn't remove {sib_dir.name}: {_e_sib}"))
        elif item.get("siblings_to_delete"):
            log.info(fmt(C.GRAY,
                f"  · Keeping {len(item['siblings_to_delete'])} sibling(s) "
                f"— partial result (n_fail={item.get('n_fail', 0)}, "
                f"n_lossy={item.get('n_lossy', 0)})"))

    if bp is not None:
        # Backup resolution uses stricter success than art/sibling cleanup —
        # the backup is the only intact copy of pre-upgrade content, so only
        # drop it when the new folder is whole (n_fail == 0 AND n_lossy == 0).
        # _item_strict_success is the download-and-import-was-clean signal,
        # independent of whether we then relocated the imported folder.
        if _item_strict_success and album_has_content:
            # bp is set only for an auto-upgrade, which wiped the only full copy,
            # so clear the same bar process.py does before deleting the backup:
            # the rebuilt folder must be verifiably at least as complete as the
            # original (track count + playtime), not merely decode-clean. The
            # artist/upgrade walks run their bulk upgrades through this executor,
            # so without this they'd skip the C01/C02 completeness gate.
            from qobuz_librarian.modes.process import _upgrade_replacement_verified
            if _upgrade_replacement_verified(item["album"], album_dir, bp):
                try:
                    shutil.rmtree(bp)
                except OSError as e:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Couldn't remove backup for {truncate(album_dir.name, 40)}: {e}"))
            else:
                pin_unverified_upgrade_backup(bp)
                log.info(fmt(C.YELLOW,
                    f"  ⚠  {truncate(album_dir.name, 40)}: upgrade couldn't be "
                    f"verified as complete — keeping your original."))
                log.info(fmt(C.GRAY,
                    f"     Original preserved at {bp} "
                    f"(kept until you confirm the upgrade landed)."))
        elif _item_strict_success:
            # The download and import succeeded cleanly, but the imported album
            # couldn't be relocated (beets renamed the folder past what the
            # matcher found), so post_dir fell back to the original we'd moved
            # aside. Restoring it now would duplicate the content beside the
            # fresh import — keep the backup and let the user reconcile.
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: imported, but the new "
                f"folder couldn't be located — keeping the backup rather than "
                f"restoring it as a duplicate."))
            log.info(fmt(C.GRAY,
                f"     Backup at {bp}; remove it once you've confirmed the "
                f"upgrade landed."))
        elif args.no_import:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: backup kept at {bp}"))
        else:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: upgrade did not succeed; restoring..."))
            if restore_upgrade_backup(bp, album_dir):
                log.info(fmt(C.GREEN, f"  ✓  Restored {truncate(album_dir.name, 50)}"))
            else:
                log.info(fmt(C.RED, f"  ✗  Auto-restore failed. Backup: {bp}"))
                log.info(fmt(C.WHITE, f"     Manual: mv {bp} {album_dir}"))

    # Gap-fill backup: present tracks moved to backup before rip.
    # Drop on success; restore in place if the queue item failed.
    gfb = item.get("gap_fill_backup_path")
    if gfb is not None and gfb.exists():
        # Drop the moved-aside present tracks only when the re-rip imported
        # cleanly — _item_strict_success requires n_fail == 0 AND n_lossy == 0,
        # so a lossy/short re-rip of a present track can't clear the original
        # (mirrors the full-album gap-fill gate in process.py). That signal is
        # independent of whether we then *located* the filled folder, which
        # matters: a clean import beets filed where the matcher can't find it
        # must NOT be treated as a failure and restored over itself.
        if _item_strict_success and album_has_content:
            try:
                shutil.rmtree(gfb)
            except OSError as e:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Gap-fill complete but couldn't remove backup: {e}"))
        elif _item_strict_success:
            # Imported cleanly, but beets filed the album where the matcher
            # couldn't find it (renamed past the fuzzy gate, or under an
            # unexpected albumartist), so post_dir fell back to the now-empty
            # album_dir. Restoring the backed-up tracks here would strand them
            # beside the fresh import and destroy the only backup — keep it and
            # let the user reconcile, exactly as the upgrade branch above does.
            log.info(fmt(C.YELLOW,
                f"  ⚠  {truncate(album_dir.name, 40)}: filled, but the new "
                f"folder couldn't be located — keeping the backed-up tracks "
                f"rather than restoring them as a duplicate."))
            log.info(fmt(C.GRAY,
                f"     Backup at {gfb}; remove it once you've confirmed the "
                f"fill landed."))
        else:
            _restore_target = album_dir or post_dir
            if _restore_target is None:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Gap-fill failed and no album dir to restore to. "
                    f"Backed-up tracks at: {gfb}"))
            else:
                if args.no_import:
                    # --no-import skips beets, so the success gate is always
                    # False here even when the download landed fine. Restoring
                    # the moved-aside present tracks is right, but it's not a
                    # failure — say so instead of "gap-fill did not succeed".
                    log.info(fmt(C.GRAY,
                        f"  · {truncate(_restore_target.name, 40)}: --no-import "
                        f"set; restoring the backed-up tracks to the library."))
                else:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  {truncate(_restore_target.name, 40)}: gap-fill "
                        f"did not succeed; restoring backed-up tracks…"))
                _n_back = restore_gap_fill_backup(gfb, _restore_target)
                if _n_back:
                    log.info(fmt(C.GREEN,
                        f"  ✓  Restored {_n_back} track(s) to "
                        f"{truncate(_restore_target.name, 50)}"))
                else:
                    log.info(fmt(C.RED,
                        f"  ✗  Couldn't restore the backed-up tracks. They're "
                        f"preserved at:\n     {gfb}"))

    n_ok = item.get("n_ok", 0)
    n_fail = item.get("n_fail", 0)
    if item.get("result") == "interrupted":
        return {"dir": album_dir, "result": "interrupted"}
    if item.get("result") == "upgrade_aborted_backup_failed":
        return {"dir": album_dir, "result": "upgrade_aborted_backup_failed"}

    if n_ok and n_fail:
        status = "partial"
    elif n_ok:
        status = "downloaded"
    elif n_fail:
        status = "failed"
    else:
        status = "nothing_landed"

    log_fetch({
        "ts": datetime.now(timezone.utc).isoformat(),
        "album_id": item["album"].get("id"),
        "artist": (item["album"].get("artist") or {}).get("name"),
        "title": item["album"].get("title"),
        "result": status,
        "tracks_total": len((item["album"].get("tracks") or {}).get("items") or []),
        "tracks_downloaded": n_ok,
        "tracks_failed": n_fail,
        "tracks_lossy_deleted": item.get("n_lossy", 0),
        "failed_titles": item.get("failed_tracks", []),
        "lossy_titles": item.get("lossy_tracks", []),
        "broken_titles": item.get("broken_tracks", []),
        "imported": imported_globally,
        "auto_upgrade": item["auto_upgrade"],
        "elapsed_s": int(item.get("elapsed", 0)),
        "queued": True,
    })
    return {
        "dir": album_dir,
        "result": status,
        "n_ok": n_ok,
        "n_fail": n_fail,
        "n_lossy": item.get("n_lossy", 0),
        "imported": imported_globally,
        "auto_upgrade": item["auto_upgrade"],
    }


def _execute_download_queue(queue, args, token, *, on_progress=None):
    """Flush a batch of pre-confirmed download decisions, one album at a time.

    Each item runs through its own pipeline — download → compress → lyrics →
    beets import (with retry on idle-timeout) → backup resolution — so a
    single broken or hung album in a large batch loses only itself instead of
    the whole import. Albums that exhaust ``BEETS_MAX_ATTEMPTS`` are parked
    under ``STAGING_DIR/<BEETS_RETRY_DIR>/`` and re-imported (no re-download)
    at the start of the next flush.

    Items are dropped from ``queue`` in place the moment they're done, so what
    remains is exactly the unfinished work: a resume re-downloads only what
    never landed and never what already imported. ``on_progress`` (when given)
    fires after each change so the caller can re-persist the shrinking queue.

    Returns ``(results, drained)``. ``results`` stays 1:1 with the items passed
    in; ``drained`` is True once nothing is left to retry.
    """
    if not queue:
        return [], True

    section(f"Download queue — {len(queue)} album(s)")

    if args.dry_run:
        log.info(fmt(C.YELLOW, "\n  --dry-run: would queue:"))
        dry_run_results = []
        for item in queue:
            tag = "↑ upgrade" if item["auto_upgrade"] else "fill"
            # Brand-new albums (missing-album / album-mode queue) have album_dir=None
            _dry_label = (item["album_dir"].name if item["album_dir"]
                          else (item["album"].get("title") or "?"))
            log.info(fmt(C.GRAY,
                f"    [{item['label']}] {tag}: {_dry_label}"))
            dry_run_results.append({"dir": item["album_dir"], "result": "dry_run",
                                    "n_missing": len(item["missing"])})
        return dry_run_results, True

    staging_preflight(args)

    items = list(queue)
    n_items = len(items)
    interrupted = False
    cancelled = False
    disk_full = False
    auth_lost_exc = None
    queue_transient_lyric_sigs = []
    any_imported = _reimport_parked_albums()
    results = []

    def _persist():
        if on_progress is not None:
            try:
                on_progress()
            except Exception as e:
                vlog(f"queue progress hook failed: {e}")

    def _drop(item):
        """An item that's fully handled leaves the queue so a retry won't redo
        it. Persist immediately, so even a hard crash mid-flush can't resurrect
        already-imported albums."""
        try:
            queue.remove(item)
        except ValueError:
            pass
        _persist()

    def _short_circuit(item):
        """Drain an item we can no longer process (cancel / interrupt / auth
        loss / disk full): mark it, run backup resolution so any upgrade backup
        gets restored, and keep it queued for a later retry. Skipping
        resolution would orphan the original tracks under their backup path."""
        item.setdefault("snapshot_before", set())
        item.setdefault("result",
                        "cancelled" if cancelled
                        else "interrupted" if interrupted
                        else "disk_full" if disk_full
                        else "auth_lost")
        results.append(_resolve_queue_item(item, args, False))

    for idx, item in enumerate(items, 1):
        if interrupted or cancelled or disk_full or auth_lost_exc is not None:
            _short_circuit(item)
            continue

        album = item["album"]
        album_dir = item["album_dir"]
        title = album.get("title") or "?"

        print()
        log.info(fmt(C.BOLD + C.WHITE,
            f"  [Q {idx}/{n_items}] {truncate(title, 55)}"))

        # Snapshot staging before the backup: a custom config can point
        # UPGRADE_BACKUP_DIR inside STAGING_DIR, and a backup copied in after the
        # snapshot would read as freshly downloaded audio. The single-album path
        # snapshots first too.
        item["snapshot_before"] = snapshot_staging()
        if item["auto_upgrade"] and album_dir and album_dir.exists():
            bp = backup_album_dir(album_dir)
            if bp is None:
                log.info(fmt(C.RED,
                    "    ✗  Could not back up; skipping this album."))
                item["result"] = "upgrade_aborted_backup_failed"
                results.append(_resolve_queue_item(item, args, False))
                continue   # nothing downloaded — stays queued for retry
            item["backup_path"] = bp
        try:
            _download_for_queue_item(item)
        except KeyboardInterrupt:
            log.info(fmt(C.YELLOW,
                "\n    Interrupted. Stopping further downloads — "
                "resolving backups for albums already processed."))
            item["result"] = "interrupted"
            interrupted = True
            results.append(_resolve_queue_item(item, args, False))
            continue
        except AuthLost as _e_auth:
            log.info(fmt(C.RED,
                "\n    Auth lost. Stopping further downloads — "
                "will restore upgrade backups and exit."))
            item["result"] = "auth_lost"
            auth_lost_exc = _e_auth
            results.append(_resolve_queue_item(item, args, False))
            continue
        except OSError as _e_os:
            if _e_os.errno != errno.ENOSPC:
                raise
            log.info(fmt(C.RED,
                f"\n    Out of disk space at {cfg.STAGING_DIR}. Stopping the "
                "queue — restoring backups and keeping the rest for a retry "
                "once space is freed."))
            item["result"] = "disk_full"
            disk_full = True
            results.append(_resolve_queue_item(item, args, False))
            continue

        if is_cancel_requested():
            from qobuz_librarian.modes.process import _discard_staged_since
            _discard_staged_since(item["snapshot_before"])
            item["result"] = "cancelled"
            cancelled = True
            log.info(fmt(C.YELLOW,
                "    Cancelled — discarded this album's partial download."))
            results.append(_resolve_queue_item(item, args, False))
            continue

        summary_n_ok = item.get("n_ok", 0)
        summary_n_fail = item.get("n_fail", 0)
        summary_elapsed = int(item.get("elapsed", 0))
        if summary_n_ok and not summary_n_fail:
            log.info(fmt(C.GREEN, f"    ✓ {summary_n_ok} track(s) · {summary_elapsed}s"))
        elif summary_n_ok:
            log.info(fmt(C.YELLOW,
                f"    ⚠ {summary_n_ok} ok · {summary_n_fail} failed · {summary_elapsed}s"))
        else:
            log.info(fmt(C.RED, f"    ✗ download failed · {summary_elapsed}s"))

        # ── Per-album pre-import + beets import ──────────────────────────
        item_imported = False
        if summary_n_ok > 0 and not args.no_import:
            album_dirs = _staged_album_dirs(item)
            if not album_dirs:
                log.info(fmt(C.YELLOW,
                    "    ⚠  No staged audio dir found for this album — skipping beets."))
            else:
                # Repair carries a callback that re-tags the staged refills
                # with the originals' own metadata before beets files them, so
                # a recording that also lives on a compilation comes back tagged
                # for the album the user owns. No-op for every other queue item.
                retag = item.get("pre_import_retag")
                if callable(retag):
                    try:
                        retag(album_dirs)
                    except Exception as _e_rt:
                        log.info(fmt(C.YELLOW,
                            f"  ⚠  repair retag step failed: {_e_rt}"))
                try:
                    sigs = _run_pre_import_hooks_for_dirs(album_dirs, args)
                    queue_transient_lyric_sigs.extend(sigs)
                except KeyboardInterrupt:
                    log.info(fmt(C.YELLOW,
                        "  ⚠  Pre-import hook interrupted — skipping beets "
                        "for this album; stopping the queue."))
                    interrupted = True
                    results.append(_resolve_queue_item(item, args, False))
                    continue

                try:
                    item_imported = _import_album_with_retry(album_dirs)
                except KeyboardInterrupt:
                    log.info(fmt(C.YELLOW,
                        "\n  beets interrupted. Resolving backups based on disk."))
                    interrupted = True
                    item_imported = False
                    results.append(_resolve_queue_item(item, args, False))
                    continue

                if item_imported:
                    any_imported = True
                else:
                    _move_to_beets_retry(album_dirs, title)
        elif args.no_import:
            log.info(fmt(C.YELLOW,
                f"  --no-import: skipping beets. Files in {cfg.STAGING_DIR}/"))
        elif summary_n_ok == 0:
            log.info(fmt(C.GRAY, "  Skipping beets — nothing landed."))

        # Drop a finished item from the persisted queue BEFORE resolving its
        # backup. If the process dies between the two, the worst case is an
        # orphaned backup the retention sweep clears — not a completed album
        # left queued and needlessly re-downloaded on the next resume. The
        # retry decision reads only download-phase state, so it's unaffected by
        # the resolve that now follows it.
        if not _queue_item_needs_retry(item):
            _drop(item)
        results.append(_resolve_queue_item(item, args, item_imported))

        # Qobuz throttles sustained bulk queues. When the last rip showed
        # throttle signals, pause longer than the normal inter-album gap
        # so we stop pounding the limit. No pause after the final album.
        if idx < n_items:
            cooldown = cfg.RATE_LIMIT_COOLDOWN if item.get("rate_limited") else 0
            if cooldown:
                log.info(fmt(C.YELLOW,
                    f"    ⏳ Qobuz rate-limit detected — cooling down "
                    f"{int(cooldown)}s before the next album "
                    f"(set RATE_LIMIT_COOLDOWN=0 to disable)."))
                time.sleep(cooldown)
            else:
                time.sleep(cfg.DELAY_BETWEEN)

    # ── Post-batch: consolidate duplicate albums once if anything landed.
    # The fold is a library-wide pass, so running it per-album would waste
    # work; once at end is enough.
    if any_imported:
        try:
            _consolidate_duplicate_albums()
        except Exception as _e_c:
            vlog(f"consolidate duplicate albums raised: {_e_c}")

    # ── Post-batch lyric-retry resolution.
    print()
    _post_dirs = [it.get("_resolved_post_dir") for it in items
                  if it.get("_resolved_post_dir") is not None]
    if (queue_transient_lyric_sigs
            and any_imported
            and auth_lost_exc is None
            and not interrupted):
        try:
            # Search the imported folders first, then the staging tree: in a
            # mixed batch, an album that imported cleanly is matched in its
            # post_dir, while one whose import failed is parked under
            # STAGING_DIR/.beets_retry/ — without the staging fallback here its
            # transient-lyric sigs would be silently dropped and never retried.
            resolved = _resolve_signatures_to_paths(
                queue_transient_lyric_sigs, _post_dirs + [cfg.STAGING_DIR])
            if resolved:
                _record_post_import_lyric_retry(resolved)
                vlog(f"lyric retry: queued {len(resolved)} "
                     f"post-import path(s) for next-launch retry")
        except Exception as _e_lr:
            vlog(f"lyric retry resolution failed: {_e_lr}")
        # Materialise .lrc sidecars next to the final renamed files
        # (no-op unless LYRICS_FORMAT is sidecar/both).
        try:
            write_post_import_sidecars(_post_dirs)
        except Exception as _e_sc:
            vlog(f"post-import sidecar write raised: {_e_sc}")
    elif (queue_transient_lyric_sigs
            and auth_lost_exc is None
            and not interrupted):
        # Some albums downloaded but none imported — files still in staging
        # (or parked under .beets_retry/). Record those paths so the
        # next-launch retry has a shot. Stale entries self-prune there.
        try:
            resolved = _resolve_signatures_to_paths(
                queue_transient_lyric_sigs, [cfg.STAGING_DIR])
            if resolved:
                _record_post_import_lyric_retry(resolved)
                vlog(f"lyric retry: no album imported; queued "
                     f"{len(resolved)} staging path(s) for next-launch retry")
        except Exception as _e_lr:
            vlog(f"lyric retry (staging fallback) failed: {_e_lr}")

    n_success = sum(1 for r in results
                    if r.get("result") in ("downloaded", "partial"))
    n_total_ok = sum(r.get("n_ok", 0) for r in results)
    n_total_fail = sum(r.get("n_fail", 0) for r in results)
    log.info(fmt(C.GREEN if n_total_fail == 0 else C.YELLOW,
        f"  ✓ Queue done: {n_success}/{n_items} albums OK · "
        f"{n_total_ok} track{'s' if n_total_ok != 1 else ''} downloaded · "
        f"{n_total_fail} failed"))

    _persist()
    if auth_lost_exc is not None:
        raise auth_lost_exc
    # Re-raising KeyboardInterrupt is what stops _flush_queue in modes 4/5
    # from reaching its clear-on-drain branch and discarding the items that
    # were short-circuited (and so still need a retry).
    if interrupted:
        raise KeyboardInterrupt
    return results, not queue
