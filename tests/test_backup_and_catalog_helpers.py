"""Tests for library/backup.py, catalog helpers, and consolidation helpers."""
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_fetch.library.backup import (
    backup_album_dir,
    backup_gap_fill_files,
    cleanup_old_upgrade_backups,
    restore_gap_fill_backup,
    restore_upgrade_backup,
)
from qobuz_fetch.library.catalog import (
    _MERGE_MAX_DEPTH,
    _merge_album_dirs,
    _sync_beets_db_after_move,
    cleanup_duplicate_art,
    maybe_remove_empty_dir,
)
from qobuz_fetch.modes.consolidate import (
    execute_consolidation,
    find_sibling_album_dirs,
    match_sibling_track,
)


class TestBackupAlbumDir:
    def test_moves_dir_to_backup(self, tmp_path, monkeypatch):
        backup_root = tmp_path / "backups"
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR", backup_root)
        album = tmp_path / "My Album"
        album.mkdir()
        (album / "track.flac").write_bytes(b"audio")
        bp = backup_album_dir(album)
        assert bp is not None and bp.exists()
        assert not album.exists()

    def test_refuses_symlinked_album_dir(self, tmp_path, monkeypatch):
        backup_root = tmp_path / "backups"
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR", backup_root)
        target = tmp_path / "real_album"
        target.mkdir()
        (target / "track.flac").write_bytes(b"audio")
        link = tmp_path / "linked_album"
        link.symlink_to(target)
        assert backup_album_dir(link) is None
        assert target.exists()

    def test_cross_filesystem_copy_verify_commit(self, tmp_path, monkeypatch):
        from qobuz_fetch.library import backup as bkmod
        backup_root = tmp_path / "backups"
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR", backup_root)
        monkeypatch.setattr(bkmod, "_same_filesystem", lambda a, b: False)
        album = tmp_path / "Album"
        album.mkdir()
        (album / "track.flac").write_bytes(b"flac" * 1000)
        bp = backup_album_dir(album)
        assert bp is not None and bp.exists()
        assert not album.exists()
        assert not (bp.with_name(bp.name + ".partial")).exists()

    def test_cross_fs_rmtree_failure_restores_original(self, tmp_path, monkeypatch):
        """When cross-FS rmtree fails partway, the original album_dir is restored from the in-place copy."""
        from qobuz_fetch.library import backup as bkmod
        backup_root = tmp_path / "backups"
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR", backup_root)
        monkeypatch.setattr(bkmod, "_same_filesystem", lambda a, b: False)
        album = tmp_path / "Album"
        album.mkdir()
        (album / "track1.flac").write_bytes(b"a" * 4096)
        (album / "track2.flac").write_bytes(b"b" * 4096)

        real_rmtree = bkmod.shutil.rmtree
        rmtree_calls = []

        def half_failing_rmtree(path, *a, **kw):
            rmtree_calls.append(str(path))
            if len(rmtree_calls) == 1 and str(path) == str(album):
                # Simulate a partial rmtree: remove one file, then fail.
                first = next(album.iterdir())
                first.unlink()
                raise OSError("device busy")
            return real_rmtree(path, *a, **kw)

        monkeypatch.setattr(bkmod.shutil, "rmtree", half_failing_rmtree)

        result = backup_album_dir(album)
        assert result is None
        assert album.exists()
        assert {p.name for p in album.iterdir()} == {"track1.flac", "track2.flac"}


class TestRestoreUpgradeBackup:
    def test_restores_backup_to_original_path(self, tmp_path):
        backup = tmp_path / "backup"
        backup.mkdir()
        (backup / "track.flac").write_bytes(b"audio")
        original = tmp_path / "original"
        assert restore_upgrade_backup(backup, original) is True
        assert original.exists()
        assert not backup.exists()

    def test_keeps_partial_when_partial_has_more_bytes_than_backup(self, tmp_path):
        """Partial with more total bytes than the backup must be kept."""
        backup = tmp_path / "backup"
        backup.mkdir()
        for i in range(5):
            (backup / f"small{i}.flac").write_bytes(b"x" * 100)
        original = tmp_path / "original"
        original.mkdir()
        (original / "huge.flac").write_bytes(b"x" * 100_000)
        assert restore_upgrade_backup(backup, original) is False
        assert (original / "huge.flac").exists()

    def test_rmtree_failure_mid_walk_preserves_backup(self, tmp_path):
        backup = tmp_path / "backup"
        backup.mkdir()
        (backup / "intact.flac").write_bytes(b"a" * 100_000)
        original = tmp_path / "original"
        original.mkdir()
        (original / "partial.flac").write_bytes(b"x" * 1_000)

        orig_rmtree = shutil.rmtree

        def fail_rmtree(*args, **kwargs):
            raise OSError("simulated mid-walk failure")

        with patch("qobuz_fetch.library.backup.shutil.rmtree",
                   side_effect=fail_rmtree):
            result = restore_upgrade_backup(backup, original)

        assert result is True
        assert (original / "intact.flac").exists()
        trash = original.with_name(original.name + ".restore_trash")
        assert trash.exists()
        orig_rmtree(str(trash))


class TestGapFillBackup:
    def test_moves_files_to_backup(self, tmp_path, monkeypatch):
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
        album = tmp_path / "album"
        album.mkdir()
        f = album / "track.flac"
        f.write_bytes(b"audio")
        bp = backup_gap_fill_files([str(f)], album)
        assert bp is not None and bp.exists()
        assert not f.exists()

    def test_falls_back_to_copy_when_rename_is_cross_device(self, tmp_path, monkeypatch):
        """Two bind mounts on one disk share a device but still reject rename with EXDEV."""
        from qobuz_fetch.library import backup as bkmod
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")

        def cross_device(*_a, **_kw):
            raise OSError("Invalid cross-device link")
        monkeypatch.setattr(bkmod.os, "rename", cross_device)

        album = tmp_path / "album"
        album.mkdir()
        src = album / "track.flac"
        src.write_bytes(b"audio-bytes")
        bp = bkmod.backup_gap_fill_files([str(src)], album)
        assert bp is not None
        assert (bp / "track.flac").read_bytes() == b"audio-bytes"
        assert not src.exists()

    def test_source_preserved_when_cross_fs_copy_fails(self, tmp_path, monkeypatch):
        from qobuz_fetch.library import backup as bkmod
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")

        def cross_device(*_a, **_kw):
            raise OSError("Invalid cross-device link")
        monkeypatch.setattr(bkmod.os, "rename", cross_device)

        def boom(*_a, **_kw):
            raise OSError("simulated disk-full during copy")
        monkeypatch.setattr(bkmod.shutil, "copy2", boom)

        album = tmp_path / "album"
        album.mkdir()
        src = album / "irreplaceable.flac"
        src.write_bytes(b"original-audio-bytes")
        bkmod.backup_gap_fill_files([str(src)], album)
        assert src.exists()
        assert src.read_bytes() == b"original-audio-bytes"

    def test_partial_failure_preserves_backup(self, tmp_path, monkeypatch):
        """A failed restore must NOT destroy the backup — it is the only surviving copy of those tracks."""
        if os.geteuid() == 0:
            pytest.skip("root bypasses the read-only perm that forces failure")
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR",
                            tmp_path / "backups")
        album = tmp_path / "album"
        album.mkdir()
        (album / "t.flac").write_bytes(b"original-audio")
        bp = backup_gap_fill_files([str(album / "t.flac")], album)
        assert bp is not None and bp.exists()
        os.chmod(album, 0o500)
        try:
            n = restore_gap_fill_backup(bp, album)
        finally:
            os.chmod(album, 0o700)
        assert n == 0
        assert bp.exists()

    def test_restore_overwrites_partial_left_by_failed_rip(self, tmp_path, monkeypatch):
        """Restore must atomically replace any partial file at the same path."""
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR",
                            tmp_path / "backups")
        album = tmp_path / "album"
        album.mkdir()
        track = album / "t.flac"
        track.write_bytes(b"the-good-original")
        bp = backup_gap_fill_files([str(track)], album)
        track.write_bytes(b"partial-junk")
        n = restore_gap_fill_backup(bp, album)
        assert n == 1
        assert track.read_bytes() == b"the-good-original"
        assert not bp.exists()

    def test_keyboardinterrupt_mid_copy_keeps_backup_and_no_tmp(
        self, tmp_path, monkeypatch
    ):
        import qobuz_fetch.library.backup as bkmod

        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR",
                            tmp_path / "backups")
        album = tmp_path / "album"
        album.mkdir()
        track = album / "t.flac"
        track.write_bytes(b"precious-audio")
        bp = backup_gap_fill_files([str(track)], album)
        assert bp is not None and bp.exists()

        def raise_ki(src, dst):
            raise KeyboardInterrupt

        monkeypatch.setattr(bkmod.shutil, "copy2", raise_ki)

        with pytest.raises(KeyboardInterrupt):
            restore_gap_fill_backup(bp, album)

        assert bp.exists(), "backup must survive an interrupted restore"
        backup_files = list(bp.rglob("*.flac"))
        assert len(backup_files) == 1, "backed-up track must still be present"
        assert not list(album.rglob("*.restore_tmp")), "no orphan restore_tmp"


class TestCleanupOldUpgradeBackups:
    def test_removes_old_backup(self, tmp_path, monkeypatch):
        backup_root = tmp_path / "backups"
        backup_root.mkdir()
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR", backup_root)
        monkeypatch.setattr("qobuz_fetch.config.DATA_DIR", tmp_path)
        old = backup_root / "20200101_120000_old_album"
        old.mkdir()
        count = cleanup_old_upgrade_backups(retention_days=1)
        assert count == 1
        assert not old.exists()

    def test_skips_dir_without_date_prefix(self, tmp_path, monkeypatch):
        """A backup dir without a YYYYMMDD_ prefix must be skipped, not deleted."""
        backup_root = tmp_path / "backups"
        backup_root.mkdir()
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR", backup_root)
        monkeypatch.setattr("qobuz_fetch.config.DATA_DIR", tmp_path)
        legacy = backup_root / "my_hand_restored_album"
        legacy.mkdir()
        os.utime(legacy, (0, 0))
        count = cleanup_old_upgrade_backups(retention_days=1)
        assert count == 0
        assert legacy.exists()

    def test_skips_resweep_within_24h(self, tmp_path, monkeypatch):
        backup_root = tmp_path / "backups"
        backup_root.mkdir()
        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR", backup_root)
        monkeypatch.setattr("qobuz_fetch.config.DATA_DIR", tmp_path)
        old = backup_root / "20200101_120000_old_album"
        old.mkdir()
        # Stamp the sweep marker recent
        (tmp_path / ".last_backup_sweep").touch()
        count = cleanup_old_upgrade_backups(retention_days=1)
        assert count == 0
        assert old.exists()
        # force=True bypasses the throttle
        assert cleanup_old_upgrade_backups(retention_days=1, force=True) == 1


class TestMaybeRemoveEmptyDir:
    def test_removes_empty_dir(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert maybe_remove_empty_dir(d) is True
        assert not d.exists()

    def test_removes_nested_empty_dirs(self, tmp_path):
        d = tmp_path / "parent"
        (d / "child").mkdir(parents=True)
        assert maybe_remove_empty_dir(d) is True
        assert not d.exists()


class TestCleanupDuplicateArt:
    def test_removes_numbered_art(self, tmp_path):
        (tmp_path / "cover.1.jpg").write_bytes(b"img")
        (tmp_path / "cover.jpg").write_bytes(b"img")
        assert cleanup_duplicate_art(tmp_path) == 1
        assert not (tmp_path / "cover.1.jpg").exists()

    def test_keeps_user_curated_multi_art_without_base(self, tmp_path):
        (tmp_path / "cover.1.jpg").write_bytes(b"booklet1")
        (tmp_path / "cover.2.jpg").write_bytes(b"booklet2")
        assert cleanup_duplicate_art(tmp_path) == 0
        assert (tmp_path / "cover.1.jpg").exists()


class TestMergeAlbumDirs:
    def test_depth_cap_prevents_runaway_recursion(self, tmp_path):
        """a deeply nested src/dst pair must bail at the depth cap rather than recurse forever."""
        src_root = tmp_path / "src"
        dst_root = tmp_path / "dst"
        depth = _MERGE_MAX_DEPTH + 5
        src_leaf, dst_leaf = src_root, dst_root
        for i in range(depth):
            src_leaf = src_leaf / f"d{i}"
            dst_leaf = dst_leaf / f"d{i}"
        src_leaf.mkdir(parents=True)
        dst_leaf.mkdir(parents=True)
        (src_leaf / "track.flac").write_bytes(b"audio")
        _merge_album_dirs(src_root, dst_root)
        assert (src_leaf / "track.flac").exists()

    def test_file_collision_replace_keeps_dst_if_move_fails(self, tmp_path, monkeypatch):
        """dst must survive intact when Path.replace raises OSError."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        src_file = src / "track.flac"
        dst_file = dst / "track.flac"
        src_file.write_bytes(b"src-audio")
        dst_file.write_bytes(b"dst-audio")

        def _bad_replace(target):
            raise OSError("injected failure")

        monkeypatch.setattr(type(src_file), "replace", lambda self, t: _bad_replace(t))

        # confirm() is imported locally in _merge_album_dirs; patch the source.
        import qobuz_fetch.ui_cli.prompts as prompts_mod
        monkeypatch.setattr(prompts_mod, "confirm", lambda *a, **kw: True)
        _merge_album_dirs(src, dst)

        # dst must still exist even though the move failed
        assert dst_file.exists()
        assert dst_file.read_bytes() == b"dst-audio"


class TestSearchLimitsRouteThroughConfig:
    """Literal `limit=N` overrides at internal call sites silently bypass
    the config knob. Every internal search call must route through
    cfg.ARTIST_LOOKUP_LIMIT or cfg.CATALOG_SEARCH_LIMIT so an operator
    can actually tune search depth."""

    def test_no_literal_search_limits_in_internal_callsites(self):
        import re
        files = [
            "src/qobuz_fetch/library/catalog.py",
            "src/qobuz_fetch/modes/artist.py",
            "src/qobuz_fetch/quality/decision.py",
            "src/qobuz_fetch/web/flows.py",
        ]
        bad = []
        # Matches: search_albums(..., limit=10) / search_artists(..., limit=5)
        pat = re.compile(r"search_(albums|artists|tracks)\([^)]*limit=\d+")
        root = Path(__file__).resolve().parents[1]
        for f in files:
            for ln, line in enumerate((root / f).read_text().splitlines(), 1):
                if pat.search(line):
                    bad.append(f"{f}:{ln}: {line.strip()}")
        assert not bad, (
            "internal callers must use cfg.ARTIST_LOOKUP_LIMIT / "
            "cfg.CATALOG_SEARCH_LIMIT, not literal limit=N:\n  "
            + "\n  ".join(bad))


class TestSyncBeetsDBAfterMove:
    """The multi-artist migration shutil-moves an album folder; beets's DB
    needs the items.path column updated to match or `beet update` will
    mark every track as deleted."""

    def _setup(self, tmp_path, monkeypatch, rows):
        import sqlite3
        music_root = tmp_path / "music"
        music_root.mkdir()
        db = tmp_path / "beets.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB)")
            for i, p in enumerate(rows, 1):
                conn.execute("INSERT INTO items (id, path) VALUES (?, ?)", (i, p))
            conn.commit()
        monkeypatch.setattr("qobuz_fetch.library.catalog.config.BEETS_DB_PATH", str(db))
        monkeypatch.setattr("qobuz_fetch.library.catalog.config.MUSIC_ROOT", music_root)
        return music_root, db

    def _read(self, db):
        import sqlite3
        with sqlite3.connect(str(db)) as conn:
            return [r[0] for r in conn.execute("SELECT path FROM items ORDER BY id")]

    def test_replaces_old_prefix_with_new(self, tmp_path, monkeypatch):
        music_root, db = self._setup(tmp_path, monkeypatch, [
            b"Atlas & Oracle, Foxing Day/Christmas Treat (2023)/01 - track.flac",
            b"Atlas & Oracle, Foxing Day/Christmas Treat (2023)/02 - other.flac",
        ])
        old = music_root / "Atlas & Oracle, Foxing Day" / "Christmas Treat (2023)"
        new = music_root / "Atlas & Oracle" / "Christmas Treat (2023)"
        # Create the new dir so `resolve()` works.
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        _sync_beets_db_after_move(old, new)
        rows = self._read(db)
        assert rows == [
            b"Atlas & Oracle/Christmas Treat (2023)/01 - track.flac",
            b"Atlas & Oracle/Christmas Treat (2023)/02 - other.flac",
        ]

    def test_unrelated_paths_unchanged(self, tmp_path, monkeypatch):
        music_root, db = self._setup(tmp_path, monkeypatch, [
            b"Atlas & Oracle, Foxing Day/Christmas Treat (2023)/01 - track.flac",
            b"The Beatles/Abbey Road (1969)/01 - Come Together.flac",
        ])
        old = music_root / "Atlas & Oracle, Foxing Day" / "Christmas Treat (2023)"
        new = music_root / "Atlas & Oracle" / "Christmas Treat (2023)"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        _sync_beets_db_after_move(old, new)
        rows = self._read(db)
        # Beatles row must stay intact.
        assert b"The Beatles/Abbey Road (1969)/01 - Come Together.flac" in rows

    def test_no_db_file_is_silent_noop(self, tmp_path, monkeypatch):
        music_root = tmp_path / "music"
        music_root.mkdir()
        monkeypatch.setattr(
            "qobuz_fetch.library.catalog.config.BEETS_DB_PATH",
            str(tmp_path / "nonexistent.db"))
        monkeypatch.setattr("qobuz_fetch.library.catalog.config.MUSIC_ROOT", music_root)
        old = music_root / "old"
        new = music_root / "new"
        old.mkdir()
        new.mkdir()
        # Must not raise.
        _sync_beets_db_after_move(old, new)


class TestMatchSiblingTrack:
    def _t(self, *, isrc="", mbid="", title="", disc=1):
        return {"isrc": isrc, "mb_trackid": mbid, "title": title, "discnumber": disc}

    def test_isrc_match(self):
        sib = self._t(isrc="USRC17607839")
        p = self._t(isrc="USRC17607839")
        assert match_sibling_track(sib, [p]) is p

    def test_isrc_beats_title(self):
        sib = self._t(isrc="AA0000000001", title="Song A", disc=1)
        p_isrc = self._t(isrc="AA0000000001", title="Song B", disc=2)
        p_title = self._t(isrc="",             title="Song A", disc=1)
        assert match_sibling_track(sib, [p_isrc, p_title]) is p_isrc

    def test_title_disc_match(self):
        sib = self._t(title="Blue Moon", disc=1)
        p = self._t(title="Blue Moon", disc=1)
        assert match_sibling_track(sib, [p]) is p

    def test_no_match_returns_none(self):
        assert match_sibling_track(self._t(title="Track A"), [self._t(title="Track B")]) is None


class TestFindSiblingAlbumDirs:
    def test_finds_remaster_sibling(self, tmp_path, monkeypatch):
        monkeypatch.setattr("qobuz_fetch.config.CONSOLIDATE_THRESH", 0.70)
        artist = tmp_path / "Artist"
        primary = artist / "Revolver"
        sibling = artist / "Revolver (2009 Remaster)"
        primary.mkdir(parents=True)
        sibling.mkdir()
        result = find_sibling_album_dirs({"title": "Revolver"}, primary)
        assert len(result) == 1 and result[0][0] == sibling

    def test_excludes_unrelated_album(self, tmp_path, monkeypatch):
        monkeypatch.setattr("qobuz_fetch.config.CONSOLIDATE_THRESH", 0.70)
        artist = tmp_path / "Artist"
        primary = artist / "Revolver"
        unrelated = artist / "Greatest Hits"
        primary.mkdir(parents=True)
        unrelated.mkdir()
        assert find_sibling_album_dirs({"title": "Revolver"}, primary) == []

    def test_sorted_by_score_desc(self, tmp_path, monkeypatch):
        monkeypatch.setattr("qobuz_fetch.config.CONSOLIDATE_THRESH", 0.70)
        artist = tmp_path / "Artist"
        primary = artist / "Revolver"
        (artist / "Revolver (Remaster)").mkdir(parents=True)
        (artist / "Revolver (Mono Mix)").mkdir()
        result = find_sibling_album_dirs({"title": "Revolver"}, primary)
        assert len(result) == 2
        scores = [r[1] for r in result]
        assert scores == sorted(scores, reverse=True)


class TestExecuteConsolidation:
    def _st(self, path):
        return {"path": str(path)}

    def _summary(self, overlap):
        return {"overlap": overlap, "unique": []}

    def test_deletes_existing_file(self, tmp_path):
        f = tmp_path / "track.flac"
        f.write_bytes(b"audio")
        n_del, n_fail = execute_consolidation(self._summary([(self._st(f), {})]))
        assert n_del == 1 and n_fail == 0
        assert not f.exists()

    def test_oserror_counted_as_failure(self, tmp_path, monkeypatch):
        f = tmp_path / "locked.flac"
        f.write_bytes(b"audio")
        import qobuz_fetch.modes.consolidate as cmod
        monkeypatch.setattr(cmod.Path, "unlink",
                            lambda self, missing_ok=False: (_ for _ in ()).throw(
                                OSError("permission denied")))
        n_del, n_fail = execute_consolidation(self._summary([(self._st(f), {})]))
        assert n_del == 0 and n_fail == 1
