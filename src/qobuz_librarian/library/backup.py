"""Upgrade and gap-fill backup/restore functions."""
import errno
import hashlib
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations.rip import flac_audio_ok
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log


def _backup_dir_name(album_dir: Path, *, kind: str = "") -> str:
    # Shared name for upgrade and gap-fill backup dirs:
    # "<ymd>_<hms>_<micro>[_<kind>]_<safe>". Microseconds keep two backups of the
    # same album in the same wall-clock second from colliding into one dir (which
    # mkdir(exist_ok=True) would silently merge, mixing two operations' files and
    # breaking the 1:1 backup→restore mapping). The retention sweep only parses
    # the leading ``\d{8}_\d{6}`` timestamp, so the extra segment is transparent.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe = re.sub(r"[^\w\-_. ]", "_", album_dir.name)[:80]
    infix = f"{kind}_" if kind else ""
    return f"{ts}_{infix}{safe}"


def _upgrade_backup_path_for(album_dir: Path) -> Path:
    cfg.UPGRADE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return cfg.UPGRADE_BACKUP_DIR / _backup_dir_name(album_dir)


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


def _file_digest(path: Path) -> str:
    """sha256 hex of a file's bytes, read in chunks so a large FLAC isn't loaded
    into memory all at once."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_FSYNC_UNSUPPORTED = {errno.EINVAL, errno.ENOTSUP,
                      getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


def _fsync(path: Path) -> bool:
    """Force a file's bytes (or a directory's entries) to stable storage, and
    report whether the flush is trustworthy.

    A copy that's read back for hashing only proves the bytes are in the page
    cache; forcing them to disk before the original is deleted is what makes a
    verified copy survive a delayed-writeback failure (ENOSPC/EIO during the lazy
    flush). Returns True on a successful flush, and also True when the mount
    simply can't fsync this object (EINVAL/ENOTSUP) — that's not a durability
    failure and must not block an otherwise-verified restore. Returns False only
    on a real flush failure, so a caller about to delete the only copy can keep
    the backup instead. Best-effort callers ignore the result."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except OSError as e:
        return e.errno in _FSYNC_UNSUPPORTED


def _fsync_tree(root: Path) -> None:
    """fsync every file and directory under ``root`` (and root itself) so a
    verified copytree is durable before the source it mirrors is removed."""
    try:
        for f in root.rglob("*"):
            if not f.is_symlink():
                _fsync(f)
    except OSError:
        pass
    _fsync(root)


def _tree_digest(d: Path):
    """{relative-path: (size, sha256)} for every regular file under tree d, or
    None on any stat/read error, an unexpected special file, or a symlink whose
    target points outside the tree.

    Content-verifies a cross-filesystem copy before the source is deleted: a
    same-size copy that differs by even one byte — a transfer glitch, a partial
    write that got re-padded — fails the match, so it can't pass as a valid
    backup and let the original be removed. A symlink whose target lives outside
    the tree isn't copied as bytes, so it can't be content-verified — refuse the
    whole backup rather than record it by target string alone."""
    out = {}
    try:
        base = d.resolve()
        for f in d.rglob("*"):
            rel = str(f.relative_to(d))
            if f.is_symlink():
                target = os.readlink(str(f))
                resolved = (Path(target) if os.path.isabs(target)
                            else f.parent / target).resolve()
                try:
                    resolved.relative_to(base)
                except ValueError:
                    return None  # target escapes the tree — can't verify its bytes
                out[rel] = ("symlink", target)
                continue
            if f.is_dir():
                continue
            if not f.is_file():
                return None  # socket/fifo/device — refuse rather than guess
            try:
                out[rel] = (f.stat().st_size, _file_digest(f))
            except OSError:
                return None
    except OSError:
        return None
    return out


# A backup records the folder it was taken from, so a sweep can tell a backup
# whose operation completed (origin rebuilt → safe to reap) from one orphaned by
# a hard kill that skipped the caller's restore/delete (origin still short → the
# backup may be the only copy). Lives inside the backup dir so rmtree clears it
# for free; restore strips it so it never lands in the live library.
_ORIGIN_SIDECAR = ".ql_backup_origin"

# Dropped into a backup when a restore left some originals behind (a partial
# restore). The leftover originals are the ONLY copy of those tracks; this marker
# says "never reap, the user must reconcile by hand." Content-presence reaping
# already keeps such a backup (the un-restored tracks aren't back at the origin),
# so this is an explicit belt-and-braces signal, not the sole protection.
_PARTIAL_RESTORE_SENTINEL = ".ql_partial_restore"

# Dropped into an upgrade backup kept because the re-rip couldn't be verified as
# complete (e.g. a track came back truncated-but-decodable, so playtime dropped).
# The backup is then the only fully-verified copy. A same-count, larger hi-res
# re-rip can leave the origin looking redundant by bytes, so the sweep needs this
# explicit "don't reap" marker on top of the content-presence proof.
_UNVERIFIED_UPGRADE_SENTINEL = ".ql_upgrade_unverified"

# Files a backup carries that aren't backed-up tracks.
_SIDECARS = (_ORIGIN_SIDECAR, _PARTIAL_RESTORE_SENTINEL, _UNVERIFIED_UPGRADE_SENTINEL)


def _write_backup_origin(bp: Path, origin: Path) -> bool:
    """Write the protective origin sidecar. Returns True only if it's actually
    on disk afterwards — the sidecar is the sole signal that keeps the age sweep
    from reaping a backup that's the only surviving copy, so a caller about to
    delete the original must treat a False here as a backup failure, not ignore
    it."""
    try:
        (bp / _ORIGIN_SIDECAR).write_text(str(origin), encoding="utf-8")
        return (bp / _ORIGIN_SIDECAR).is_file()
    except OSError:
        return False


def _read_backup_origin(bp: Path):
    f = bp / _ORIGIN_SIDECAR
    try:
        return Path(f.read_text(encoding="utf-8").strip()) if f.is_file() else None
    except OSError:
        return None


def _backup_safe_to_reap(bp: Path) -> bool:
    """True ONLY when ``bp`` is provably redundant — every track it holds is
    confirmed back at its origin. The age sweep reaps on this, so the burden of
    proof is on "safe to delete", not on "must keep": any uncertainty (no
    sidecar, unreadable origin, a keep marker, a track not proven back) means we
    cannot prove redundancy and the backup is KEPT.

    Redundancy is proved by CONTENT, not file count: every track in the backup
    must be back at the origin under the same relative path and at least as
    large. A bare count match is fooled when restored or gap-filled files
    inflate the origin's count while one of the backup's own tracks is still
    missing or short there — exactly the case that strands the only good copy.

    This is deliberately the inverse of a "protect if marked" scheme. A backup
    can become the only surviving copy whenever the originals were moved into it
    and not fully put back, and the protective sidecar/sentinel writes are
    best-effort — on the exact filesystem failures that strand a sole copy
    (ENOSPC, RO remount, EACCES) those writes can themselves fail. Making
    "keep" the default means no protective write has to succeed for the data to
    be safe; the worst case of a missing marker is a stranded backup the user
    clears by hand, never silent loss."""
    # Explicit keep markers: a partial restore, or an upgrade kept because it
    # couldn't be verified complete — never reap either.
    if (bp / _PARTIAL_RESTORE_SENTINEL).is_file() or (bp / _UNVERIFIED_UPGRADE_SENTINEL).is_file():
        return False
    try:
        tracks = [f for f in bp.rglob("*") if f.is_file() and f.name not in _SIDECARS]
    except OSError:
        return False                       # can't read the backup → can't prove redundant
    if not tracks:
        return True                        # holds no tracks → nothing to lose → reap the husk
    origin = _read_backup_origin(bp)
    if origin is None or not origin.exists():
        return False                       # can't locate origin → can't prove redundant
    for f in tracks:
        try:
            dst = origin / f.relative_to(bp)
            if not dst.is_file() or dst.stat().st_size < f.stat().st_size:
                return False               # this track isn't proven back at the origin
        except OSError:
            return False
    return True


def pin_unverified_upgrade_backup(bp: Path) -> None:
    """Mark an upgrade backup as the only fully-verified copy so the age sweep
    leaves it alone. Best-effort: content-presence reaping already keeps a backup
    whose tracks aren't all proven back at the origin, but a same-count larger
    hi-res re-rip can defeat the byte check, so this is the explicit signal."""
    try:
        (bp / _UNVERIFIED_UPGRADE_SENTINEL).write_text(
            "upgrade kept — replacement not verified complete; the only full copy",
            encoding="utf-8")
    except OSError:
        pass


# Memoize the orphan walk: every settings load/submit and the dashboard call
# _diagnostics(), which calls this, and it rglob-walks every retained backup to
# content-check redundancy. A burst of those hits (form POST → redirect →
# dashboard render) would otherwise re-walk the whole backup tree each time. The
# cache is invalidated when the backup dir's mtime changes (a backup added or
# removed bumps it) or when the short TTL lapses (catches a sub-dir-only change,
# e.g. a restore completing, that doesn't touch the parent's mtime).
_ONLY_COPY_TTL_SEC = 10.0
_only_copy_cache: tuple[float, float, list] | None = None
# Executor threads (retention) and the web diagnostic both call
# find_only_copy_backups; the lock keeps the memo read-modify-write atomic and
# stops two callers re-walking the tree at once on a cache miss.
_only_copy_lock = threading.Lock()


def find_only_copy_backups():
    """Backups whose recorded origin is gone or still short of them — orphaned
    by a hard kill that skipped the caller's restore/delete. Retention keeps
    these; the web diagnostic surfaces them so the user can recover or clear
    them (each holds the origin path in its sidecar).

    Memoized for a few seconds keyed on the backup dir's mtime — see
    _ONLY_COPY_TTL_SEC — so repeated diagnostics don't each re-walk the tree."""
    global _only_copy_cache
    if not cfg.UPGRADE_BACKUP_DIR.exists():
        with _only_copy_lock:
            _only_copy_cache = None
        return []
    try:
        dir_mtime = cfg.UPGRADE_BACKUP_DIR.stat().st_mtime
    except OSError:
        dir_mtime = 0.0
    now = time.time()
    with _only_copy_lock:
        cached = _only_copy_cache
        if (cached is not None and cached[1] == dir_mtime
                and now - cached[0] < _ONLY_COPY_TTL_SEC):
            return cached[2]

        out = []
        try:
            for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
                if not entry.is_dir():
                    continue
                # A genuine mid-copy '.partial' (sidecar not yet written) isn't a
                # real backup; a committed backup whose album name merely ends in
                # '.partial' DOES carry the origin sidecar and must still surface.
                if entry.name.endswith(".partial") and not (entry / _ORIGIN_SIDECAR).is_file():
                    continue
                if not _backup_safe_to_reap(entry):
                    out.append((entry, _read_backup_origin(entry)))
        except OSError:
            pass
        _only_copy_cache = (now, dir_mtime, out)
        return out


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
            # Pure rename only — NOT shutil.move. Two bind mounts of the same
            # host disk share an st_dev (so _same_filesystem says "same"), yet
            # os.rename across them raises EXDEV; shutil.move would then silently
            # fall back to an UNVERIFIED copytree+rmtree. Catch EXDEV and fall
            # through to the verified cross-fs path below instead.
            os.rename(str(album_dir), str(bp))
        except OSError as e:
            if e.errno != errno.EXDEV:
                from qobuz_librarian.ui_cli.errors import oserr_hint
                log.info(fmt(C.RED,
                    f"  ✗  Could not back up {album_dir}: {e}.{oserr_hint(e)}"))
                return None
            # EXDEV: same st_dev but cross-mount — use the verified copy path.
        else:
            if not _write_backup_origin(bp, album_dir):
                # The move already emptied album_dir, so bp is now the only
                # copy — but with no sidecar the age sweep could reap it. Undo
                # the move (put the album back) and report failure rather than
                # leave an unprotected sole copy.
                log.info(fmt(C.RED,
                    f"  ✗  Couldn't record backup origin for {album_dir.name}; "
                    "moving the album back and aborting backup."))
                try:
                    shutil.move(str(bp), str(album_dir))
                except (OSError, shutil.Error) as e2:
                    log.info(fmt(C.RED,
                        f"  ✗  Couldn't restore {album_dir} after the failed "
                        f"origin write: {e2}. Files are at {bp}."))
                return None
            return bp

    # Cross-filesystem (or same-st_dev-but-EXDEV) copy-verify-commit-remove.
    src_stats = _tree_stats(album_dir)
    if src_stats is None:
        log.info(fmt(C.RED,
            f"  ✗  Couldn't stat source tree at {album_dir}; refusing to back up."))
        return None
    n_files, total_bytes = src_stats
    # Content fingerprint of the source, taken before the copy: the post-copy
    # check compares bytes (sha256), not just (count, total_bytes), so a
    # same-size corruption can't pass as a valid backup and let the original be
    # deleted.
    src_digest = _tree_digest(album_dir)
    if src_digest is None:
        log.info(fmt(C.RED,
            f"  ✗  Couldn't read source tree at {album_dir} (unreadable or a "
            "special file); refusing to back up."))
        return None
    log.info(fmt(C.GRAY,
        f"  ⤷  Cross-filesystem backup: copying {n_files} file(s) / "
        f"{total_bytes / 1024 / 1024:.1f} MB to {cfg.UPGRADE_BACKUP_DIR}…"))

    bp_partial = bp.with_name(bp.name + ".partial")
    try:
        shutil.copytree(str(album_dir), str(bp_partial), symlinks=True)
        if _tree_digest(bp_partial) != src_digest:
            log.info(fmt(C.RED,
                "  ✗  Backup verification failed: the copy doesn't match the "
                "source byte-for-byte. Refusing to proceed."))
            shutil.rmtree(str(bp_partial), ignore_errors=True)
            return None
        # Force the copy to stable storage before the original is removed below,
        # then commit atomically (same-fs rename within UPGRADE_BACKUP_DIR).
        _fsync_tree(bp_partial)
        os.rename(str(bp_partial), str(bp))
        _fsync(bp.parent)
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

    # Backup is committed — record its origin BEFORE the destructive remove.
    # If the rmtree (and the auto-restore below) then fail, the album is
    # half-deleted and this backup is its only intact copy; without the sidecar
    # the age-cleanup sweep can't tell it's protected and would reap it. So if
    # the sidecar can't be written, do NOT remove the original — keep both
    # copies and report failure rather than risk an unprotected sole copy.
    if not _write_backup_origin(bp, album_dir):
        log.info(fmt(C.RED,
            f"  ✗  Backup copied but couldn't record its origin; leaving the "
            f"original at {album_dir} in place and discarding the backup."))
        shutil.rmtree(str(bp), ignore_errors=True)
        return None
    # Remove the original; on failure, the rmtree may have already deleted some
    # files, leaving album_dir in a partial state that a later scan would
    # mis-treat. Restore from the backup we just made so the caller is back to
    # the pre-call state.
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
    bp = cfg.UPGRADE_BACKUP_DIR / _backup_dir_name(album_dir, kind="gapfill")
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
                # Content-verify the copy (sha256) before unlinking the source,
                # so a same-size transfer corruption can't delete the original.
                src_digest = _file_digest(src)
                shutil.copy2(str(src), str(dst))
                if _file_digest(dst) != src_digest:
                    try:
                        dst.unlink()
                    except OSError:
                        pass
                    raise OSError("copy content mismatch (verification failed)")
                _fsync(dst)
                _fsync(dst.parent)
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
    # The sidecar is the only thing that keeps the age sweep from reaping this
    # backup once it becomes a sole copy (a repair that doesn't restore). If it
    # can't be written, move the backed-up originals home and fail rather than
    # hand back an unprotected backup.
    if not _write_backup_origin(bp, album_dir):
        log.info(fmt(C.RED,
            f"  ✗  Couldn't record gap-fill backup origin for {album_dir.name}; "
            "restoring the originals and aborting backup."))
        restore_gap_fill_backup(bp, album_dir, keep_larger_dst=False)

        def _still_has_tracks():
            try:
                return any(p.is_file() and p.name != _ORIGIN_SIDECAR
                           for p in bp.rglob("*"))
            except OSError:
                return True  # can't tell → assume tracks remain, don't lose them
        if _still_has_tracks():
            # Restore couldn't put every original back, so bp is the only copy
            # of what's left. Hand it back so the caller records it and recovery
            # stays reachable, and retry the sidecar so the age sweep protects
            # it — returning None here would orphan these files.
            _write_backup_origin(bp, album_dir)
            log.info(fmt(C.RED,
                f"  ✗  Couldn't restore all originals to {album_dir}; the "
                f"surviving copies are kept at {bp} (the only copy)."))
            return bp
        return None  # everything restored to album_dir; bp is empty
    return bp


def restore_gap_fill_backup(backup_path: Path, album_dir: Path,
                            *, keep_larger_dst: bool = True) -> int:
    """Move every file in backup_path back to its original location under
    album_dir, preserving relative structure. Returns the number of files
    restored. Removes the backup dir on completion. Safe to call on a
    non-existent or empty backup_path (returns 0).

    keep_larger_dst (default True, the repair caller): when a file already at
    the destination is >= the backup copy in bytes, keep it and discard the
    backup — valid for repair, where the backup is the truncated original and
    a larger dst is the good refill. Gap-fill callers pass False: there the
    backup IS the good original, so a larger-but-corrupt partial re-rip at dst
    must NOT win — always restore the backup.

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
        if not f.is_file() or f.name in _SIDECARS:
            continue
        rel = f.relative_to(backup_path)
        dst = album_dir / rel
        # Never clobber a destination that already holds at least as many bytes
        # as the backup copy. The backup here is the pre-repair (truncated)
        # original, which is by definition SMALLER than a good refill; if a
        # fresh, larger file already sits at dst (e.g. a refill beets imported
        # under the same name in a same-ISRC/dedup edge), restoring the smaller
        # original over it would be a downgrade. Leave the good file in place.
        try:
            if (keep_larger_dst and dst.exists()
                    and dst.stat().st_size >= f.stat().st_size
                    and flac_audio_ok(dst) is not False):
                # dst is larger AND decodes, so it's the good refill — keep it.
                # "Larger" alone isn't "good": a bigger-but-corrupt partial must
                # not win over the backed-up original, so a dst that fails to
                # decode falls through to the verified restore below instead of
                # discarding the backup. (flac_audio_ok is None when there's no
                # flac tool to check with — then trust the size, as before.)
                log.info(fmt(C.GRAY,
                    f"  · Keeping the file already at {dst.name} "
                    f"(>= the backed-up copy) rather than restoring over it."))
                try:
                    f.unlink()
                except OSError:
                    pass
                n_restored += 1
                continue
        except OSError:
            pass
        tmp = dst.with_name(dst.name + ".restore_tmp")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Copy onto the destination filesystem first, then swap in
            # atomically. os.replace is atomic when tmp and dst share a
            # filesystem (tmp lives in dst.parent, so they do) and it
            # overwrites any partial the failed rip left at dst.
            # This backup file is the ONLY copy (originals were moved here), so
            # content-verify the copy (sha256) BEFORE the destructive swap. A
            # short OR same-size-corrupt copy that copy2 didn't raise on
            # (truncating/overlay FS, quota cutoff, sparse mismatch) must not
            # overwrite dst and then delete the source — that would destroy the
            # only good copy. Mirrors backup_gap_fill_files / restore_upgrade_backup,
            # which already content-verify; a size check alone misses a same-size
            # corruption.
            src_digest = _file_digest(f)
            shutil.copy2(str(f), str(tmp))
            if _file_digest(tmp) != src_digest:
                tmp.unlink(missing_ok=True)
                n_failed += 1
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Couldn't restore {f.name}: copy verification failed "
                    f"(content mismatch) — kept the backup copy."))
                continue
            # Force the copy down before the swap, and the swap before the backup
            # copy is deleted — this is the only copy of the track. A flush that
            # genuinely fails (ENOSPC/EIO) means the bytes aren't safely on disk,
            # so keep the backup rather than swap-and-delete on top of an
            # unflushed copy. (_fsync still returns True when the mount simply
            # can't fsync, so this never blocks a normal restore.)
            if not _fsync(tmp):
                tmp.unlink(missing_ok=True)
                n_failed += 1
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Couldn't restore {f.name}: the copy couldn't be "
                    f"flushed to disk — kept the backup copy."))
                continue
            os.replace(str(tmp), str(dst))
            _fsync(dst.parent)
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
        # Mark it so the age sweep never reaps this backup — after a partial
        # restore the leftover files are the only copy, but the file-count
        # heuristic can't tell (the restored files now inflate the origin).
        try:
            (backup_path / _PARTIAL_RESTORE_SENTINEL).write_text(
                "partial restore — un-restored originals are the only copy",
                encoding="utf-8")
        except OSError:
            pass
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

            # Strictly bigger only: a byte-equal partial isn't "more data" to
            # restore, so leave it rather than churn the live dir on a tie — both
            # copies are kept for the user to reconcile.
            if bk_bytes > orig_bytes:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Replacing partial dir ({orig_files} file(s), "
                    f"{orig_bytes / 1024 / 1024:.1f} MB) with backup "
                    f"({bk_files} file(s), {bk_bytes / 1024 / 1024:.1f} MB)."))
                # A partial re-rip that beets had already imported leaves rows
                # in the beets DB pointing at files we're about to wipe. Drop
                # them first so the DB doesn't end up referencing deleted
                # paths (the bug that bit --force re-downloads landing with
                # any fail/lossy: the restore put the backup back over a
                # partial import, the DB rows for the partial lingered as
                # ghost tracks). Lazy-imported to keep the library layer free
                # of an integrations import at module load.
                try:
                    from qobuz_librarian.integrations.beets import (
                        forget_beets_entries,
                    )
                    partial_files = [p for p in original_path.rglob("*")
                                     if p.is_file()]
                    if partial_files:
                        n_forgotten = forget_beets_entries(partial_files)
                        if n_forgotten:
                            log.info(fmt(C.GRAY,
                                f"     · Dropped {n_forgotten} stale beets "
                                "entry/entries for the partial-import paths."))
                except Exception as e:
                    # Forget is best-effort: if beets is missing or the DB
                    # is locked, restore still proceeds. The user only sees
                    # ghost entries until a manual `beet update`.
                    log.info(fmt(C.YELLOW,
                        f"  · Couldn't pre-clear partial-import entries from "
                        f"beets ({e}); they may show as ghosts until "
                        "`beet update`."))
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
        # Crash-safe restore. /upgrade_backups → /music can be a different mount
        # in Docker, and even when _same_filesystem() reports the same st_dev an
        # os.rename across two bind mounts of one host disk still raises EXDEV. So
        # try a pure rename first — NOT shutil.move, which on EXDEV would silently
        # degrade to an UNVERIFIED copytree+rmtree and could leave a half-written
        # original on an ENOSPC mid-copy (the very failure this restore exists to
        # prevent). On EXDEV fall through to a verified copy: stage onto the
        # destination fs, check the tree matches, atomically rename it in, and
        # only then drop the backup.
        renamed = False
        if _same_filesystem(backup_path, original_path):
            try:
                os.rename(str(backup_path), str(original_path))
                renamed = True
            except OSError as e:
                if e.errno != errno.EXDEV:
                    raise
        if not renamed:
            restoring = original_path.with_name(original_path.name + ".restoring")
            if restoring.exists():
                shutil.rmtree(str(restoring), ignore_errors=True)
            src_digest = _tree_digest(backup_path)
            shutil.copytree(str(backup_path), str(restoring), symlinks=True)
            if src_digest is None or _tree_digest(restoring) != src_digest:
                shutil.rmtree(str(restoring), ignore_errors=True)
                log.info(fmt(C.RED,
                    f"  ✗  Restore verification failed; backup kept at "
                    f"{backup_path}.\n     Manual restore: mv {backup_path!s} "
                    f"{original_path!s}"))
                return False
            # Durable before the backup (the only copy) is removed below.
            _fsync_tree(restoring)
            os.rename(str(restoring), str(original_path))
            _fsync(original_path.parent)
            shutil.rmtree(str(backup_path), ignore_errors=True)
        try:
            for sidecar in _SIDECARS:
                (original_path / sidecar).unlink(missing_ok=True)
        except OSError:
            pass
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
        if entry.name.endswith(".partial") and not (entry / _ORIGIN_SIDECAR).is_file():
            # Stranded mid-copy dir from a hard kill during a cross-fs backup.
            # A committed backup ALWAYS writes the origin sidecar (even one whose
            # album name itself ends in '.partial'), so its absence marks a
            # never-finished copy. Only reap once it's old enough that it can't
            # be another process's actively-running cross-fs copy (also
            # sidecar-less until it commits) — the run lock usually serialises
            # this, but the grace window is cheap insurance.
            try:
                stale = (time.time() - entry.stat().st_mtime) > 3600
            except OSError:
                stale = True
            if stale:
                try:
                    shutil.rmtree(entry)
                    n_removed += 1
                except OSError:
                    pass
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
            if not _backup_safe_to_reap(entry):
                # We can't PROVE this backup is redundant (origin gone, a track
                # not back at it, unreadable, or an explicit keep marker), so it
                # may be the only copy of the tracks it holds. Retention must
                # never reap the last copy; keep it and let the web diagnostic
                # surface it for the user to reconcile (restore or remove by hand).
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Keeping backup {entry.name!r} past retention — can't "
                    f"confirm its tracks are back in the original folder."))
                continue
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
