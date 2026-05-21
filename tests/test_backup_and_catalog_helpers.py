"""Tests for backup/restore safety, beets-DB-after-move sync, and the
consolidation helpers. These mostly cover the cross-filesystem / interrupted-
operation paths that have actually bit us — the gnarly parts."""
import os
import shutil
import sqlite3
from unittest.mock import patch

import pytest

from qobuz_librarian.library.backup import (
    backup_album_dir,
    backup_gap_fill_files,
    cleanup_old_upgrade_backups,
    restore_gap_fill_backup,
    restore_upgrade_backup,
)
from qobuz_librarian.library.catalog import (
    _MERGE_MAX_DEPTH,
    _merge_album_dirs,
    _sync_beets_db_after_merge,
    _sync_beets_db_after_move,
    cleanup_duplicate_art,
    maybe_remove_empty_dir,
)
from qobuz_librarian.modes.consolidate import (
    execute_consolidation,
    find_sibling_album_dirs,
    match_sibling_track,
)

# ── backup_album_dir ─────────────────────────────────────────────────────────

def test_backup_album_dir_moves_and_refuses_symlinks(tmp_path, monkeypatch):
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    album = tmp_path / "My Album"
    album.mkdir()
    (album / "track.flac").write_bytes(b"audio")
    bp = backup_album_dir(album)
    assert bp is not None and bp.exists() and not album.exists()

    # A symlinked album dir must be refused — the upgrade replace would
    # otherwise wipe the target the user actually links into.
    target = tmp_path / "real_album"
    target.mkdir()
    (target / "track.flac").write_bytes(b"audio")
    link = tmp_path / "linked_album"
    link.symlink_to(target)
    assert backup_album_dir(link) is None
    assert target.exists()


def test_backup_album_dir_cross_filesystem_copy_verify_commit(tmp_path, monkeypatch):
    # When src and backup are on different filesystems, rename can't be used —
    # backup copies, verifies, then deletes the source. No .partial must survive.
    from qobuz_librarian.library import backup as bkmod
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bkmod, "_same_filesystem", lambda a, b: False)
    album = tmp_path / "Album"
    album.mkdir()
    (album / "track.flac").write_bytes(b"flac" * 1000)
    bp = backup_album_dir(album)
    assert bp is not None and bp.exists() and not album.exists()
    assert not bp.with_name(bp.name + ".partial").exists()


def test_backup_album_dir_cross_fs_rmtree_failure_restores_original(tmp_path, monkeypatch):
    # Cross-FS path: copy succeeds, then rmtree fails partway. The original
    # must be restored from the still-intact copy rather than left half-deleted.
    from qobuz_librarian.library import backup as bkmod
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bkmod, "_same_filesystem", lambda a, b: False)
    album = tmp_path / "Album"
    album.mkdir()
    (album / "track1.flac").write_bytes(b"a" * 4096)
    (album / "track2.flac").write_bytes(b"b" * 4096)

    real_rmtree = bkmod.shutil.rmtree
    calls = []

    def half_failing(path, *a, **kw):
        calls.append(str(path))
        if len(calls) == 1 and str(path) == str(album):
            next(album.iterdir()).unlink()
            raise OSError("device busy")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(bkmod.shutil, "rmtree", half_failing)
    assert backup_album_dir(album) is None
    assert album.exists()
    assert {p.name for p in album.iterdir()} == {"track1.flac", "track2.flac"}


# ── restore_upgrade_backup ──────────────────────────────────────────────────

def test_restore_upgrade_backup_keeps_a_bigger_partial(tmp_path):
    # A partial that's already larger than the backup must NOT be replaced —
    # we'd be downgrading the user's data to the stale snapshot.
    backup = tmp_path / "backup"
    backup.mkdir()
    for i in range(5):
        (backup / f"small{i}.flac").write_bytes(b"x" * 100)
    original = tmp_path / "original"
    original.mkdir()
    (original / "huge.flac").write_bytes(b"x" * 100_000)
    assert restore_upgrade_backup(backup, original) is False
    assert (original / "huge.flac").exists()


def test_restore_upgrade_backup_survives_rmtree_failure_mid_walk(tmp_path):
    # The partial-removal rmtree fails — backup must NOT be deleted; it's
    # the only surviving copy.
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "intact.flac").write_bytes(b"a" * 100_000)
    original = tmp_path / "original"
    original.mkdir()
    (original / "partial.flac").write_bytes(b"x" * 1_000)

    with patch("qobuz_librarian.library.backup.shutil.rmtree",
               side_effect=OSError("simulated mid-walk failure")):
        assert restore_upgrade_backup(backup, original) is True
    assert (original / "intact.flac").exists()
    # The aborted partial gets parked in a .restore_trash dir for the user
    # to clean — should be present, but the backup itself is consumed.
    shutil.rmtree(original.with_name(original.name + ".restore_trash"))


def test_restore_upgrade_backup_clears_a_stale_restore_trash(tmp_path):
    # A prior interrupted restore can leave a .restore_trash beside the album;
    # it must be cleared, or it blocks the rename here (and orphans forever).
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "intact.flac").write_bytes(b"a" * 100_000)
    original = tmp_path / "Album"
    original.mkdir()
    (original / "partial.flac").write_bytes(b"x" * 1_000)
    stale = original.with_name(original.name + ".restore_trash")
    stale.mkdir()
    (stale / "old.flac").write_bytes(b"x" * 500)

    assert restore_upgrade_backup(backup, original) is True
    assert (original / "intact.flac").exists()
    assert not stale.exists()


# ── backup_gap_fill_files ───────────────────────────────────────────────────

def test_gap_fill_backup_falls_back_to_copy_on_cross_device_rename(tmp_path, monkeypatch):
    # Two bind mounts on one disk share a device but still reject rename
    # with EXDEV — the backup must fall back to a copy.
    from qobuz_librarian.library import backup as bkmod
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bkmod.os, "rename",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("Invalid cross-device link")))
    album = tmp_path / "album"
    album.mkdir()
    src = album / "track.flac"
    src.write_bytes(b"audio-bytes")
    bp = bkmod.backup_gap_fill_files([str(src)], album)
    assert bp is not None and (bp / "track.flac").read_bytes() == b"audio-bytes"
    assert not src.exists()


def test_gap_fill_backup_preserves_source_when_copy_fails(tmp_path, monkeypatch):
    # If both rename AND the copy fallback fail, the source must still be
    # there — losing it would be irrecoverable.
    from qobuz_librarian.library import backup as bkmod
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bkmod.os, "rename",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("EXDEV")))
    monkeypatch.setattr(bkmod.shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    album = tmp_path / "album"
    album.mkdir()
    src = album / "irreplaceable.flac"
    src.write_bytes(b"original-audio-bytes")
    bkmod.backup_gap_fill_files([str(src)], album)
    assert src.read_bytes() == b"original-audio-bytes"


def test_gap_fill_restore_handles_failure_partial_and_interrupt(tmp_path, monkeypatch):
    # Three failure modes in one — the common invariant is "backup must survive".
    import qobuz_librarian.library.backup as bkmod
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")

    # 1) Restore against a read-only target fails cleanly without destroying the backup.
    if os.geteuid() != 0:
        album = tmp_path / "ro_album"
        album.mkdir()
        (album / "t.flac").write_bytes(b"original")
        bp = backup_gap_fill_files([str(album / "t.flac")], album)
        os.chmod(album, 0o500)
        try:
            assert restore_gap_fill_backup(bp, album) == 0
        finally:
            os.chmod(album, 0o700)
        assert bp.exists()

    # 2) Restore atomically overwrites a partial junk file left by a failed rip.
    album2 = tmp_path / "partial_album"
    album2.mkdir()
    track = album2 / "t.flac"
    track.write_bytes(b"the-good-original")
    bp = backup_gap_fill_files([str(track)], album2)
    track.write_bytes(b"partial-junk")
    assert restore_gap_fill_backup(bp, album2) == 1
    assert track.read_bytes() == b"the-good-original"
    assert not bp.exists()

    # 3) KeyboardInterrupt mid-copy leaves the backup intact + no .restore_tmp.
    album3 = tmp_path / "ki_album"
    album3.mkdir()
    tr = album3 / "t.flac"
    tr.write_bytes(b"precious-audio")
    bp = backup_gap_fill_files([str(tr)], album3)
    monkeypatch.setattr(bkmod.shutil, "copy2",
                        lambda src, dst: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        restore_gap_fill_backup(bp, album3)
    assert bp.exists() and len(list(bp.rglob("*.flac"))) == 1
    assert not list(album3.rglob("*.restore_tmp"))


# ── cleanup_old_upgrade_backups ────────────────────────────────────────────

def test_cleanup_old_upgrade_backups_respects_dates_and_throttle(tmp_path, monkeypatch):
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", backup_root)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", tmp_path)

    old = backup_root / "20200101_120000_old_album"
    old.mkdir()
    legacy = backup_root / "my_hand_restored_album"  # no date prefix
    legacy.mkdir()
    os.utime(legacy, (0, 0))

    # First sweep: deletes the dated, leaves the legacy folder.
    assert cleanup_old_upgrade_backups(retention_days=1) == 1
    assert not old.exists() and legacy.exists()

    # 24h throttle prevents a re-sweep — recreate and confirm.
    old.mkdir()
    assert cleanup_old_upgrade_backups(retention_days=1) == 0
    assert old.exists()
    # force=True bypasses the throttle.
    assert cleanup_old_upgrade_backups(retention_days=1, force=True) == 1


# ── catalog helpers: maybe_remove_empty_dir / cleanup_duplicate_art ─────────

def test_maybe_remove_empty_dir_walks_nested():
    # Just confirm the recursive case — the trivial single-dir case is implied.
    from pathlib import Path
    from tempfile import mkdtemp
    d = Path(mkdtemp())
    try:
        nested = d / "parent" / "child"
        nested.mkdir(parents=True)
        assert maybe_remove_empty_dir(d / "parent") is True
        assert not (d / "parent").exists()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cleanup_duplicate_art_only_drops_when_a_base_exists(tmp_path):
    # cover.jpg + cover.1.jpg → the .1 is dropped as a duplicate.
    (tmp_path / "cover.1.jpg").write_bytes(b"img")
    (tmp_path / "cover.jpg").write_bytes(b"img")
    assert cleanup_duplicate_art(tmp_path) == 1
    assert not (tmp_path / "cover.1.jpg").exists()

    # No base cover.jpg → user-curated booklet pages; keep both.
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "cover.1.jpg").write_bytes(b"booklet1")
    (extra / "cover.2.jpg").write_bytes(b"booklet2")
    assert cleanup_duplicate_art(extra) == 0
    assert (extra / "cover.1.jpg").exists()


# ── _merge_album_dirs ──────────────────────────────────────────────────────

def test_merge_album_dirs_caps_depth_and_keeps_dst_on_replace_failure(tmp_path, monkeypatch):
    # Deeply nested src/dst pair bails at the depth cap — no infinite recursion.
    src_root = tmp_path / "src_deep"
    dst_root = tmp_path / "dst_deep"
    src_leaf, dst_leaf = src_root, dst_root
    for i in range(_MERGE_MAX_DEPTH + 5):
        src_leaf = src_leaf / f"d{i}"
        dst_leaf = dst_leaf / f"d{i}"
    src_leaf.mkdir(parents=True)
    dst_leaf.mkdir(parents=True)
    (src_leaf / "track.flac").write_bytes(b"audio")
    _merge_album_dirs(src_root, dst_root)
    assert (src_leaf / "track.flac").exists()

    # File collision where Path.replace raises must NOT lose dst.
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "track.flac").write_bytes(b"src-audio")
    (dst / "track.flac").write_bytes(b"dst-audio")
    monkeypatch.setattr(type(src / "x"), "replace",
                        lambda self, t: (_ for _ in ()).throw(OSError("injected")))
    import qobuz_librarian.ui_cli.prompts as prompts_mod
    monkeypatch.setattr(prompts_mod, "confirm", lambda *a, **kw: True)
    _merge_album_dirs(src, dst)
    assert (dst / "track.flac").read_bytes() == b"dst-audio"


# ── _sync_beets_db_after_move + _after_merge ───────────────────────────────

def _setup_beets_db(tmp_path, monkeypatch, rows):
    music_root = tmp_path / "music"
    music_root.mkdir()
    db = tmp_path / "beets.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB)")
        for i, p in enumerate(rows, 1):
            conn.execute("INSERT INTO items (id, path) VALUES (?, ?)", (i, p))
        conn.commit()
    monkeypatch.setattr("qobuz_librarian.library.catalog.config.BEETS_DB_PATH", str(db))
    monkeypatch.setattr("qobuz_librarian.library.catalog.config.MUSIC_ROOT", music_root)
    return music_root, db


def _read_beets_paths(db):
    with sqlite3.connect(str(db)) as conn:
        return [r[0] for r in conn.execute("SELECT path FROM items ORDER BY id")]


def test_sync_beets_db_after_move_repoints_paths_and_leaves_others_alone(tmp_path, monkeypatch):
    music_root, db = _setup_beets_db(tmp_path, monkeypatch, [
        b"Atlas & Oracle, Foxing Day/Christmas Treat (2023)/01 - track.flac",
        b"Atlas & Oracle, Foxing Day/Christmas Treat (2023)/02 - other.flac",
        b"The Beatles/Abbey Road (1969)/01 - Come Together.flac",
    ])
    old = music_root / "Atlas & Oracle, Foxing Day" / "Christmas Treat (2023)"
    new = music_root / "Atlas & Oracle" / "Christmas Treat (2023)"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    _sync_beets_db_after_move(old, new)
    rows = _read_beets_paths(db)
    assert b"Atlas & Oracle/Christmas Treat (2023)/01 - track.flac" in rows
    assert b"Atlas & Oracle/Christmas Treat (2023)/02 - other.flac" in rows
    # Unrelated row left intact.
    assert b"The Beatles/Abbey Road (1969)/01 - Come Together.flac" in rows


def test_sync_beets_db_after_move_silently_skips_when_db_absent(tmp_path, monkeypatch):
    music_root = tmp_path / "music"
    music_root.mkdir()
    monkeypatch.setattr("qobuz_librarian.library.catalog.config.BEETS_DB_PATH",
                        str(tmp_path / "nonexistent.db"))
    monkeypatch.setattr("qobuz_librarian.library.catalog.config.MUSIC_ROOT", music_root)
    old = music_root / "old"
    new = music_root / "new"
    old.mkdir()
    new.mkdir()
    _sync_beets_db_after_move(old, new)  # must not raise


def test_sync_beets_db_after_merge_drops_collisions_and_repoints_the_rest(tmp_path, monkeypatch):
    music_root, db = _setup_beets_db(tmp_path, monkeypatch, [
        b"Primary, Other/Album (2020)/01 - a.flac",   # collides with dst row
        b"Primary, Other/Album (2020)/02 - b.flac",   # unique -> repointed
        b"Primary/Album (2020)/01 - a.flac",          # pre-existing dst row
    ])
    old = music_root / "Primary, Other" / "Album (2020)"
    new = music_root / "Primary" / "Album (2020)"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    _sync_beets_db_after_merge(old, new)
    rows = _read_beets_paths(db)
    assert sorted(rows) == sorted([
        b"Primary/Album (2020)/01 - a.flac",
        b"Primary/Album (2020)/02 - b.flac",
    ])
    assert rows.count(b"Primary/Album (2020)/01 - a.flac") == 1


# ── consolidation helpers ──────────────────────────────────────────────────

def test_match_sibling_track_isrc_beats_title():
    t = lambda **kw: {"isrc": "", "mb_trackid": "", "title": "", "discnumber": 1, **kw}
    # Plain ISRC match.
    sib = t(isrc="USRC17607839")
    p = t(isrc="USRC17607839")
    assert match_sibling_track(sib, [p]) is p
    # ISRC wins even when the title-disc pair points elsewhere.
    sib = t(isrc="AA0000000001", title="Song A", disc=1)
    p_isrc = t(isrc="AA0000000001", title="Song B", disc=2)
    p_title = t(title="Song A", disc=1)
    assert match_sibling_track(sib, [p_isrc, p_title]) is p_isrc
    # No identifying overlap → None, not a guess.
    assert match_sibling_track(t(title="Track A"), [t(title="Track B")]) is None


def test_find_sibling_album_dirs_finds_remasters_and_sorts(tmp_path, monkeypatch):
    monkeypatch.setattr("qobuz_librarian.config.CONSOLIDATE_THRESH", 0.70)
    artist = tmp_path / "Artist"
    primary = artist / "Revolver"
    primary.mkdir(parents=True)
    (artist / "Revolver (Remaster)").mkdir()
    (artist / "Revolver (Mono Mix)").mkdir()
    (artist / "Greatest Hits").mkdir()  # unrelated — must not appear
    result = find_sibling_album_dirs({"title": "Revolver"}, primary)
    titles = [r[0].name for r in result]
    assert "Greatest Hits" not in titles
    assert len(result) == 2
    assert [r[1] for r in result] == sorted([r[1] for r in result], reverse=True)


def test_execute_consolidation_deletes_and_counts_failures(tmp_path, monkeypatch):
    f_ok = tmp_path / "track.flac"
    f_ok.write_bytes(b"audio")
    f_locked = tmp_path / "locked.flac"
    f_locked.write_bytes(b"audio")
    summary = {"overlap": [({"path": str(f_ok)}, {}), ({"path": str(f_locked)}, {})],
               "unique": []}

    import qobuz_librarian.modes.consolidate as cmod
    real_unlink = cmod.Path.unlink

    def maybe_fail(self, missing_ok=False):
        if self.name == "locked.flac":
            raise OSError("permission denied")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(cmod.Path, "unlink", maybe_fail)
    deleted, n_fail = execute_consolidation(summary)
    assert [p.name for p in deleted] == ["track.flac"] and n_fail == 1
    assert not f_ok.exists() and f_locked.exists()
