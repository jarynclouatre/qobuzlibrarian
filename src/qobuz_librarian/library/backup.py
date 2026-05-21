"""Upgrade and gap-fill backup/restore functions.

"""
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log


def _upgrade_backup_path_for(album_dir: Path) -> Path:
    cfg.UPGRADE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\-_. ]", "_", album_dir.name)[:80]
    return cfg.UPGRADE_BACKUP_DIR / f"{ts}_{safe}"


def _same_filesystem(a: Path, b: Path) -> bool:
    """True if a and b live on the same filesystem (same st_dev).

    Inside Docker, /music and /upgrade_backups are separate bind mounts
    so they get different st_dev even when the host paths share a disk —
    that's what makes the cross-fs path the common case for image users.
    Walks up to the nearest existing ancestor on either side so this can
    answer before either dir actually exists.
    """
    def _existing_ancestor(p: Path) -> Path:
        cur = p
        while not cur.exists() and cur != cur.parent:
            cur = cur.parent
        return cur
    try:
        return os.stat(_existing_ancestor(a)).st_dev == os.stat(_existing_ancestor(b)).st_dev
    except OSError:
        return False


def _tree_stats(d: Path):
    """(file_count, total_bytes) for tree d, or None on stat error."""
    n_files = 0
    n_bytes = 0
    try:
        for f in d.rglob("*"):
            if f.is_file():
                n_files += 1
                try:
                    n_bytes += f.stat().st_size
                except OSError:
                    return None
    except OSError:
        return None
    return (n_files, n_bytes)


def backup_album_dir(album_dir: Path):
    """Move album_dir to a timestamped backup. Returns the backup Path on
    success, None on failure. Refuses symlinks (rename would orphan the
    target). Cross-fs uses copy-verify-commit-remove so a mid-copy abort
    leaves the original intact."""
    if not album_dir.exists():
        return None
    if album_dir.is_symlink():
        log.info(fmt(C.RED,
            f"  ✗  Refusing to back up a symlinked album dir: {album_dir}\n"
            f"     Resolve the symlink (move/copy the target into the music tree) "
            f"and re-run, or use --no-upgrade to skip this album."))
        return None
    try:
        bp = _upgrade_backup_path_for(album_dir)
    except OSError as e:
        log.info(fmt(C.RED, f"  ✗  Couldn't prepare backup path: {e}."))
        return None

    if _same_filesystem(album_dir, cfg.UPGRADE_BACKUP_DIR):
        try:
            shutil.move(str(album_dir), str(bp))
            return bp
        except (OSError, shutil.Error) as e:
            from qobuz_librarian.ui_cli.errors import oserr_hint
            hint = oserr_hint(e) if isinstance(e, OSError) else ""
            log.info(fmt(C.RED,
                f"  ✗  Could not back up {album_dir}: {e}.{hint}"))
            return None

    # Cross-filesystem copy-verify-commit-remove.
    src_stats = _tree_stats(album_dir)
    if src_stats is None:
        log.info(fmt(C.RED,
            f"  ✗  Couldn't stat source tree at {album_dir}; refusing to back up."))
        return None
    n_files, total_bytes = src_stats
    log.info(fmt(C.GRAY,
        f"  ⤷  Cross-filesystem backup: copying {n_files} file(s) / "
        f"{total_bytes / 1024 / 1024:.1f} MB to {cfg.UPGRADE_BACKUP_DIR}…"))

    bp_partial = bp.with_name(bp.name + ".partial")
    try:
        shutil.copytree(str(album_dir), str(bp_partial), symlinks=True)
        dst_stats = _tree_stats(bp_partial)
        if dst_stats != src_stats:
            log.info(fmt(C.RED,
                f"  ✗  Backup verification failed: source {src_stats} vs "
                f"copy {dst_stats}. Refusing to proceed."))
            shutil.rmtree(str(bp_partial), ignore_errors=True)
            return None
        # Atomic commit (same-fs rename within UPGRADE_BACKUP_DIR).
        os.rename(str(bp_partial), str(bp))
    except KeyboardInterrupt:
        log.info(fmt(C.YELLOW,
            f"\n  ⚠  Backup interrupted mid-copy. Original at {album_dir} "
            f"is intact; removing partial backup."))
        shutil.rmtree(str(bp_partial), ignore_errors=True)
        raise
    except (OSError, shutil.Error) as e:
        log.info(fmt(C.RED, f"  ✗  Cross-filesystem backup failed: {e}."))
        shutil.rmtree(str(bp_partial), ignore_errors=True)
        return None

    # Backup is committed. Remove the original; on failure, the rmtree
    # may have already deleted some files, leaving album_dir in a partial
    # state that a later scan would mis-treat. Restore from the backup
    # we just made so the caller is back to the pre-call state.
    try:
        shutil.rmtree(str(album_dir))
    except OSError as e:
        log.info(fmt(C.RED,
            f"  ✗  Backup at {bp} succeeded but couldn't remove original: {e}."))
        log.info(fmt(C.YELLOW,
            "     Restoring original from backup so the album dir isn't "
            "left half-deleted…"))
        if restore_upgrade_backup(bp, album_dir):
            log.info(fmt(C.GREEN,
                f"  ✓  Restored {album_dir}; backup discarded."))
            return None
        log.info(fmt(C.RED,
            f"     Auto-restore also failed. {album_dir} may be partial; "
            f"backup retained at {bp}."))
        log.info(fmt(C.RED,
            f"     Manual: rm -rf {album_dir} && mv {bp} {album_dir}"))
        return None
    return bp


def backup_gap_fill_files(file_paths, album_dir: Path):
    """Move a subset of files (the about-to-be-replaced gap-fill tracks) to
    a timestamped backup dir, preserving their relative paths within
    album_dir (so multi-disc structure is kept). Returns backup Path if
    at least one file was successfully backed up, None otherwise.

    Per-file: same-fs uses an atomic rename; cross-fs uses
    copy → size verification → unlink-source so a copy failure leaves
    the source intact. If any individual file can't be backed up the
    source is preserved on disk (beets may then create a .N.flac
    collision on import — annoying but recoverable), never silently
    deleted.

    Without this, a rip failure mid-download (network drop, Ctrl+C,
    auth loss) leaves the user with permanent track loss: the originals
    were deleted and the new versions never arrived. The backup gives
    the caller a recovery path."""
    cfg.UPGRADE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\-_. ]", "_", album_dir.name)[:80]
    bp = cfg.UPGRADE_BACKUP_DIR / f"{ts}_gapfill_{safe}"
    try:
        bp.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't create gap-fill backup dir ({e}); "
            "proceeding without backup safety net."))
        return None

    for fp in file_paths:
        src = Path(fp)
        if not src.exists():
            continue
        try:
            rel = src.relative_to(album_dir)
        except ValueError:
            rel = Path(src.name)
        dst = bp / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(str(src), str(dst))
            except OSError:
                # Two bind mounts of the same host disk share an st_dev but
                # rename across them still raises EXDEV, so don't trust a
                # same-filesystem guess here — fall back to copy + verify
                # size + remove source. A mid-copy failure leaves the
                # source intact.
                src_size = src.stat().st_size
                shutil.copy2(str(src), str(dst))
                # Read the size before unlinking — re-stat'ing dst after the
                # unlink would raise FileNotFoundError and mask the real reason.
                dst_size = dst.stat().st_size
                if dst_size != src_size:
                    try:
                        dst.unlink()
                    except OSError:
                        pass
                    raise OSError(f"copy size mismatch ({dst_size} != {src_size})")
                src.unlink()
        except (OSError, shutil.Error) as e:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Couldn't back up {src.name} ({e}); leaving in place. "
                f"Expect a beets .N.flac collision on this track."))
            # Clean any partial copy we may have left at dst.
            try:
                if dst.exists():
                    dst.unlink()
            except OSError:
                pass
    # If no files actually landed (every move failed, only empty per-disc dirs
    # were created), drop the dir and return None — otherwise the caller reads a
    # non-empty path as "backup succeeded" when it holds no tracks.
    try:
        if not any(p.is_file() for p in bp.rglob("*")):
            shutil.rmtree(bp, ignore_errors=True)
            return None
    except OSError:
        pass
    return bp


def restore_gap_fill_backup(backup_path: Path, album_dir: Path) -> int:
    """Move every file in backup_path back to its original location under
    album_dir, preserving relative structure. Returns the number of files
    restored. Removes the backup dir on completion. Safe to call on a
    non-existent or empty backup_path (returns 0).

    Crash-safe across filesystems: each file is copied to a
    ``.restore_tmp`` sibling *on the destination filesystem*, then
    atomically ``os.replace``d into place (which also overwrites any
    partial the failed rip left at that path), and only then is the
    backup copy removed. An interrupt mid-copy leaves the backup intact
    and at worst an orphan ``.restore_tmp`` next to the destination —
    never a half-written destination with the backup already gone."""
    if backup_path is None or not backup_path.exists():
        return 0
    try:
        album_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.info(fmt(C.RED,
            f"  ✗  Couldn't recreate album dir for restore: {e}\n"
            f"     Backed-up tracks remain at: {backup_path}"))
        return 0
    n_restored = 0
    n_failed = 0
    for f in backup_path.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(backup_path)
        dst = album_dir / rel
        tmp = dst.with_name(dst.name + ".restore_tmp")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Copy onto the destination filesystem first, then swap in
            # atomically. os.replace is atomic when tmp and dst share a
            # filesystem (tmp lives in dst.parent, so they do) and it
            # overwrites any partial the failed rip left at dst.
            shutil.copy2(str(f), str(tmp))
            os.replace(str(tmp), str(dst))
            # Destination is verifiably in place — the backup copy is now
            # redundant. A failure to unlink it here is non-fatal: the
            # end-of-function rmtree clears the whole backup dir.
            try:
                f.unlink()
            except OSError:
                pass
            n_restored += 1
        except KeyboardInterrupt:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except (OSError, shutil.Error) as e:
            n_failed += 1
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            log.info(fmt(C.YELLOW, f"  ⚠  Couldn't restore {f.name}: {e}."))
    # CRITICAL: this is the only surviving copy of these tracks (the
    # originals were moved here by backup_gap_fill_files). Only delete the
    # backup once EVERY file is safely back. If any move failed, deleting
    # it would permanently destroy precisely the tracks we failed to
    # restore — on the path that exists to prevent data loss. Preserve it
    # and tell the user where it is.
    if n_failed:
        log.info(fmt(C.RED + C.BOLD,
            f"  ✗  {n_failed} track(s) could NOT be restored. Originals are "
            f"PRESERVED at:\n     {backup_path}\n"
            f"     Move them back by hand once the cause is resolved."))
    else:
        try:
            shutil.rmtree(backup_path)
        except OSError:
            pass
    return n_restored


def restore_upgrade_backup(backup_path: Path, original_path: Path) -> bool:
    """Move a backup back to its original location. If a partial download
    left a sparse album dir at original_path, automatically replace it with
    the backup (the backup is the only intact copy). Returns True on success.

    Compares backup vs partial on TOTAL BYTES, not file count: a partial
    download might have grabbed the single largest track first (1 huge
    file) while the legitimate backup holds the rest (many smaller
    files). File-count alone would call the backup "bigger" and wipe the
    intact track. Bytes-based is what actually matters for "more data
    here than there".
    """
    if not backup_path.exists():
        return False
    try:
        if original_path.exists():
            # A partial download / failed beets import may have left a sparse
            # album dir at the original path. The backup is the only intact
            # copy; if it has more BYTES than the partial dir, the right move
            # is to overwrite the partial with the backup.
            bk_stats = _tree_stats(backup_path)
            orig_stats = _tree_stats(original_path)
            if bk_stats is None or orig_stats is None:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Couldn't read tree stats for restore decision.\n"
                    f"     Backup is at: {backup_path}\n"
                    f"     Manual restore: rm -rf {original_path!s} && "
                    f"mv {backup_path!s} {original_path!s}"))
                return False
            bk_files, bk_bytes = bk_stats
            orig_files, orig_bytes = orig_stats

            if bk_bytes >= orig_bytes:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Replacing partial dir ({orig_files} file(s), "
                    f"{orig_bytes / 1024 / 1024:.1f} MB) with backup "
                    f"({bk_files} file(s), {bk_bytes / 1024 / 1024:.1f} MB)."))
                # Wipe partial in two phases so a mid-walk failure leaves
                # a recognisable trash dir rather than a half-deleted album
                # path that beets would later collide with.
                trash = original_path.with_name(original_path.name + ".restore_trash")
                if trash.exists():
                    # A prior interrupted restore can leave this behind; clear it
                    # now so it both stops being an orphan and doesn't block the
                    # rename below (which would fail onto a non-empty dir).
                    try:
                        shutil.rmtree(str(trash))
                    except OSError:
                        pass
                try:
                    os.rename(str(original_path), str(trash))
                except OSError as e:
                    log.info(fmt(C.RED,
                        f"  ✗  Couldn't move partial aside: {e}\n"
                        f"     Backup is at: {backup_path}\n"
                        f"     Manual restore: rm -rf {original_path!s} && "
                        f"mv {backup_path!s} {original_path!s}"))
                    return False
                try:
                    shutil.rmtree(str(trash))
                except OSError as e:
                    # Partial trash dir left behind. The original path is
                    # clear (renamed away) so the upcoming move will
                    # succeed; surface the leftover so the user can clean.
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  Couldn't fully wipe old partial at {trash} "
                        f"({e}); restore continuing. Remove that dir by hand."))
            else:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Cannot auto-restore: {original_path} has "
                    f"{orig_files} file(s) / {orig_bytes / 1024 / 1024:.1f} MB "
                    f"(backup has {bk_files} / {bk_bytes / 1024 / 1024:.1f} MB).\n"
                    f"     Backup is at: {backup_path}\n"
                    f"     Manual restore: rm -rf {original_path!s} && "
                    f"mv {backup_path!s} {original_path!s}"))
                return False
        original_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup_path), str(original_path))
        return True
    except (OSError, shutil.Error) as e:
        log.info(fmt(C.RED,
            f"  ✗  Restore failed: {e}.\n"
            f"     Backup is preserved at: {backup_path}\n"
            f"     Manual restore: rm -rf {original_path!s} && "
            f"mv {backup_path!s} {original_path!s}"))
        return False


def cleanup_old_upgrade_backups(retention_days: int = None,
                                force: bool = False) -> int:
    """Sweep upgrade-backup dir of anything older than retention_days.
    Called once at script startup. Returns count of dirs removed.

    Parses the timestamp prefix encoded in each backup dir's
    name (YYYYMMDD_HHMMSS_safe) instead of stat().st_mtime. shutil.move
    preserves the source's mtime, so a fresh backup of an old folder
    inherited that old mtime — and was being auto-deleted on the very
    next run despite being just minutes old. Skips (does not delete)
    backups whose names don't parse (legacy / hand-named / hand-restored).

    Stamps DATA_DIR/.last_backup_sweep and skips a re-sweep within 24h
    unless ``force=True`` — a CLI session that opens and closes ten times
    in a minute shouldn't stat the whole backup dir each time.
    """
    if retention_days is None:
        retention_days = cfg.UPGRADE_BACKUP_RETENTION_DAYS
    if not cfg.UPGRADE_BACKUP_DIR.exists():
        return 0
    sweep_stamp = cfg.DATA_DIR / ".last_backup_sweep"
    if not force and sweep_stamp.exists():
        try:
            if (time.time() - sweep_stamp.stat().st_mtime) < 86400:
                return 0
        except OSError:
            pass
    cutoff = time.time() - (retention_days * 86400)
    n_removed = 0
    for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
        if not entry.is_dir():
            continue
        m = re.match(r"^(\d{8}_\d{6})_", entry.name)
        if not m:
            log.info(fmt(C.YELLOW,
                f"  ⚠  upgrade-backup dir {entry.name!r} has no timestamp "
                f"prefix; leaving it alone (manual removal required)."))
            continue
        try:
            ts = datetime.strptime(m.group(1),
                                   "%Y%m%d_%H%M%S").timestamp()
        except ValueError:
            log.info(fmt(C.YELLOW,
                f"  ⚠  upgrade-backup dir {entry.name!r} has an unparseable "
                f"timestamp prefix; leaving it alone."))
            continue
        if ts < cutoff:
            try:
                shutil.rmtree(entry)
                n_removed += 1
            except OSError:
                pass
    try:
        sweep_stamp.parent.mkdir(parents=True, exist_ok=True)
        sweep_stamp.touch()
    except OSError:
        pass
    return n_removed
