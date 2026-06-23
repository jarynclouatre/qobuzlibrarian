"""Tests for backup/restore safety, beets-DB-after-move sync, and the
consolidation helpers. These mostly cover the cross-filesystem / interrupted-
operation paths that have actually bit us — the gnarly parts."""
import errno
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
    _sync_beets_db_after_move,
)
from qobuz_librarian.modes.consolidate import (
    execute_consolidation,
    match_sibling_track,
)


def _need_audio_tools():
    if not (shutil.which("ffmpeg") and shutil.which("flac")):
        pytest.skip("ffmpeg/flac not available")


def _real_flac(path, *, seconds=2):
    """Encode a short white-noise FLAC that actually decodes with ``flac -t``."""
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"anoisesrc=duration={seconds}:color=white:amplitude=0.5",
         "-ac", "2", "-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
         str(path)], check=True)


# ── backup_album_dir ─────────────────────────────────────────────────────────

def test_cross_fs_backup_rejects_same_size_corruption(tmp_path, monkeypatch):
    # A cross-filesystem backup must content-verify the copy, not just match
    # (file count, total bytes). A same-size but different-content copy — a silent
    # transfer corruption — must be rejected and the original left intact, never
    # deleted as a corrupt sole backup.
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    # Force the cross-filesystem copy-verify path (tmp_path is one filesystem).
    monkeypatch.setattr("qobuz_librarian.library.backup._same_filesystem",
                        lambda a, b: False)
    album = tmp_path / "Album (2026)"
    album.mkdir()
    original = b"REAL-FLAC-AUDIO-CONTENT"
    (album / "01.flac").write_bytes(original)

    real_copytree = shutil.copytree

    def corrupt_copytree(src, dst, *a, **k):
        real_copytree(src, dst, *a, **k)
        for f in (tmp_path / "backups").rglob("*"):
            if f.is_file():
                f.write_bytes(b"\x00" * f.stat().st_size)  # same size, wrong bytes
        return dst

    monkeypatch.setattr("qobuz_librarian.library.backup.shutil.copytree",
                        corrupt_copytree)
    bp = backup_album_dir(album)
    assert bp is None                                     # verification rejected the copy
    assert (album / "01.flac").read_bytes() == original   # source preserved, not deleted


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


def test_restore_overwrites_partial_but_keeps_larger_good_file(tmp_path):
    # restore_gap_fill_backup must overwrite a SMALLER partial at the
    # destination (its recovery purpose) but must NOT clobber a LARGER good file
    # already there (the same-ISRC/dedup edge where a fresh refill landed under
    # the same name — the backed-up original is the truncated, smaller one).
    import qobuz_librarian.library.backup as bk

    # Smaller partial at dst -> overwritten by the full backup copy.
    a = tmp_path / "a"
    (a / "bk").mkdir(parents=True)
    (a / "album").mkdir()
    (a / "bk" / "01.flac").write_bytes(b"FULL-ORIGINAL-CONTENT-XXXXXX")  # 27B
    (a / "album" / "01.flac").write_bytes(b"partial")                    # 7B
    assert bk.restore_gap_fill_backup(a / "bk", a / "album") == 1
    assert (a / "album" / "01.flac").read_bytes() == b"FULL-ORIGINAL-CONTENT-XXXXXX"

    # Larger good import at dst -> kept, not downgraded by the truncated backup.
    _need_audio_tools()
    b = tmp_path / "b"
    (b / "bk").mkdir(parents=True)
    (b / "album").mkdir()
    (b / "bk" / "01.flac").write_bytes(b"trunc")                          # 5B
    _real_flac(b / "album" / "01.flac")          # a real, larger, decodable file
    good = (b / "album" / "01.flac").read_bytes()
    assert bk.restore_gap_fill_backup(b / "bk", b / "album") == 1
    assert (b / "album" / "01.flac").read_bytes() == good
    assert not (b / "bk").exists()               # backup discarded


def test_restore_does_not_keep_larger_but_corrupt_dst(tmp_path):
    # keep_larger_dst trusts "larger" only when the destination actually decodes.
    # A bigger-but-undecodable file at dst (a re-padded partial / corrupt refill)
    # must NOT win over the backed-up original: the backup is restored over it so
    # the only good copy isn't dropped on a byte count alone.
    import qobuz_librarian.library.backup as bk
    _need_audio_tools()
    c = tmp_path / "c"
    (c / "bk").mkdir(parents=True)
    (c / "album").mkdir()
    _real_flac(c / "bk" / "01.flac", seconds=2)          # good original (backup)
    good = (c / "bk" / "01.flac").read_bytes()
    # Corrupt file at dst, larger in bytes than the good backup but won't decode.
    (c / "album" / "01.flac").write_bytes(b"\x00" * (len(good) + 4096))
    assert bk.restore_gap_fill_backup(c / "bk", c / "album") == 1
    assert (c / "album" / "01.flac").read_bytes() == good   # restored, not kept


def test_age_sweep_keeps_any_backup_it_cannot_prove_redundant(tmp_path, monkeypatch):
    # Safety-first reaping: a backup is deleted ONLY when its origin is confirmed
    # to hold its tracks. A backup with NO sidecar at all (every protective write
    # failed) must still be KEPT, not reaped — so no protective write is
    # load-bearing for data safety.
    import qobuz_librarian.library.backup as bk
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", backup_root)
    monkeypatch.setattr(bk.cfg, "DATA_DIR", tmp_path)

    # Aged backup holding a track, with NO sidecar and NO sentinel (simulating
    # every protective write having failed on a full/RO disk).
    bp = backup_root / "20200101_000000_naked"
    bp.mkdir()
    (bp / "01.flac").write_bytes(b"x" * 5000)
    assert not bk._backup_safe_to_reap(bp)        # can't prove redundant → keep
    removed = bk.cleanup_old_upgrade_backups(retention_days=1, force=True)
    assert bp.exists() and (bp / "01.flac").exists()
    assert removed == 0
    # And it's surfaced to the user for reconciliation.
    assert any(e == bp for e, _origin in bk.find_only_copy_backups())


def test_unverified_upgrade_backup_is_pinned_from_age_sweep(tmp_path, monkeypatch):
    # An upgrade kept because it couldn't be verified complete (a truncated-but-
    # decodable track drops playtime) leaves the backup as the only full copy.
    # The re-rip can land at the same names but larger hi-res bytes, so content
    # alone reads as redundant — the explicit pin must keep the sweep off it.
    import qobuz_librarian.library.backup as bk
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bk.cfg, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()

    album = tmp_path / "music" / "Album (2020)"
    album.mkdir(parents=True)
    (album / "01 - A.flac").write_bytes(b"a" * 3000)
    (album / "02 - B.flac").write_bytes(b"b" * 3000)
    bp = bk.backup_album_dir(album)
    assert bp is not None

    album.mkdir(parents=True, exist_ok=True)
    (album / "01 - A.flac").write_bytes(b"A" * 9000)
    (album / "02 - B.flac").write_bytes(b"B" * 9000)
    assert bk._backup_safe_to_reap(bp)          # by content/bytes alone, redundant
    bk.pin_unverified_upgrade_backup(bp)
    assert not bk._backup_safe_to_reap(bp)      # pinned → kept

    aged = bp.with_name("20200101_000000_aged")
    bp.rename(aged)
    assert bk.cleanup_old_upgrade_backups(force=True) == 0
    assert (aged / "01 - A.flac").exists()


def test_backup_refuses_rather_than_leave_unprotected_sole_copy(tmp_path, monkeypatch):
    # The origin sidecar is the only thing that stops the age sweep reaping a
    # backup that's a sole copy. If it can't be written, the backup helpers must
    # NOT hand back an unprotected backup with the originals deleted — they put
    # the files back and report failure.
    import qobuz_librarian.library.backup as bk
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bk, "_write_backup_origin", lambda bp, origin: False)

    # backup_album_dir (same-fs move path): album must be back on disk, no backup.
    album = tmp_path / "music" / "Album (2020)"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"audio-1")
    (album / "02.flac").write_bytes(b"audio-2")
    assert bk.backup_album_dir(album) is None
    assert (album / "01.flac").read_bytes() == b"audio-1"   # restored intact
    assert (album / "02.flac").exists()
    assert not any((tmp_path / "backups").glob("*")) if (tmp_path / "backups").exists() else True

    # backup_gap_fill_files: the moved-aside originals must be restored, no backup.
    g1 = album / "01.flac"
    before = g1.read_bytes()
    assert bk.backup_gap_fill_files([str(g1)], album) is None
    assert g1.exists() and g1.read_bytes() == before


# ── restore_upgrade_backup ──────────────────────────────────────────────────

def test_restore_upgrade_backup_forgets_partial_import_paths_from_beets(tmp_path):
    # The R2 bug: --force routes through the auto-upgrade restore branch; if
    # the re-rip lands with any failed tracks, beets has imported the partial
    # files into the library and recorded their paths. The restore overwrites
    # those files with the backup, leaving the beets DB pointing at deleted
    # paths (ghost rows). Restore now calls forget_beets_entries on whatever's
    # under the partial dir BEFORE wiping it.
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "intact1.flac").write_bytes(b"a" * 100_000)
    (backup / "intact2.flac").write_bytes(b"a" * 100_000)

    original = tmp_path / "original"
    original.mkdir()
    partial_one = original / "partial1.flac"
    partial_two = original / "partial2.flac"
    partial_one.write_bytes(b"x" * 500)
    partial_two.write_bytes(b"x" * 500)

    captured_paths = []

    def fake_forget(paths):
        captured_paths.extend(str(p) for p in paths)
        return 2  # pretend beets had two entries it dropped

    with patch("qobuz_librarian.integrations.beets.forget_beets_entries",
               side_effect=fake_forget):
        assert restore_upgrade_backup(backup, original) is True

    # The partial files' paths went through forget_beets_entries before the
    # wipe, so the DB rows beets had for them are gone — no ghosts.
    assert set(captured_paths) == {str(partial_one), str(partial_two)}
    # And the restore still landed: intact files are back at the original.
    assert (original / "intact1.flac").exists()


def test_restore_upgrade_backup_exdev_verifies_before_dropping_backup(tmp_path, monkeypatch):
    # Same-st_dev bind mounts can still raise EXDEV on rename. The restore must
    # use the verified copy-to-.restoring path, NOT shutil.move's unverified
    # copy+delete: if the staged copy doesn't match the backup, the backup must
    # be PRESERVED, not deleted. Reproduces the data-loss-on-restore hole.
    import qobuz_librarian.library.backup as bkmod
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "track1.flac").write_bytes(b"a" * 50_000)
    (backup / "track2.flac").write_bytes(b"b" * 50_000)
    original = tmp_path / "Album"  # absent → straight to the move branch

    # Same st_dev, yet rename fails cross-mount like two bind mounts of one disk.
    monkeypatch.setattr(bkmod, "_same_filesystem", lambda a, b: True)
    real_rename = bkmod.os.rename

    def fake_rename(src, dst, *a, **k):
        if str(src) == str(backup):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(bkmod.os, "rename", fake_rename)

    # Simulate an interrupted/short copy: only one of the two tracks lands.
    def short_copytree(src, dst, *a, **k):
        os.makedirs(dst, exist_ok=True)
        shutil.copy2(os.path.join(src, "track1.flac"),
                     os.path.join(dst, "track1.flac"))
        return dst

    monkeypatch.setattr(bkmod.shutil, "copytree", short_copytree)

    # Verified path catches the short copy: keeps the backup, reports failure,
    # leaves no half-written original. (shutil.move would have copied+deleted.)
    assert restore_upgrade_backup(backup, original) is False
    assert backup.exists()
    assert (backup / "track1.flac").exists() and (backup / "track2.flac").exists()
    assert not original.exists()
    assert not original.with_name(original.name + ".restoring").exists()


# ── backup_gap_fill_files ───────────────────────────────────────────────────

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
    import qobuz_librarian.library.backup as bk
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    music = tmp_path / "music"
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", backup_root)
    monkeypatch.setattr("qobuz_librarian.config.DATA_DIR", tmp_path)

    # A dated, PROVABLY-REDUNDANT backup: its origin exists with >= as many
    # files, so the sweep can confirm the content was put back and reap it.
    def _make_completed(name):
        bp = backup_root / name
        bp.mkdir()
        (bp / "01.flac").write_bytes(b"a" * 100)
        origin = music / name.split("_", 2)[-1]
        origin.mkdir(parents=True, exist_ok=True)
        (origin / "01.flac").write_bytes(b"a" * 9000)  # rebuilt, full
        bk._write_backup_origin(bp, origin)
        return bp
    old = _make_completed("20200101_120000_old_album")
    legacy = backup_root / "my_hand_restored_album"  # no date prefix
    legacy.mkdir()
    os.utime(legacy, (0, 0))

    # First sweep: reaps the dated completed backup, leaves the legacy folder.
    assert cleanup_old_upgrade_backups(retention_days=1) == 1
    assert not old.exists() and legacy.exists()

    # 24h throttle prevents a re-sweep — recreate and confirm.
    old = _make_completed("20200101_120000_old_album")
    assert cleanup_old_upgrade_backups(retention_days=1) == 0
    assert old.exists()
    # force=True bypasses the throttle.
    assert cleanup_old_upgrade_backups(retention_days=1, force=True) == 1


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


# ── consolidation helpers ──────────────────────────────────────────────────

def test_match_sibling_track_requires_duration_to_confirm_a_duplicate():
    # Title + disc + position is not recording identity without an ISRC/MBID — a
    # distinct recording sharing the slot (a live setlist replayed on another
    # date) must never be deleted. Matching now requires the durations to agree,
    # for tagged AND untagged tracks; file size is too weak a tiebreak to use.
    t = lambda **kw: {"isrc": "", "mb_trackid": "", "title": "Intro",
                      "discnumber": 1, "tracknumber": 0, "length": 0.0,
                      "size": 0, **kw}
    # Same duration → duplicate.
    assert match_sibling_track(t(length=30.0), [t(length=30.4)]) is not None
    # Different duration → distinct recording → kept.
    assert match_sibling_track(t(length=30.0), [t(length=120.0)]) is None
    # No readable duration on either side → no evidence → kept, even when the
    # file sizes happen to be close (the old size fallback is gone).
    assert match_sibling_track(t(length=0.0, size=5_000_000),
                               [t(length=0.0, size=5_050_000)]) is None
    # Tagged: same slot but DIFFERENT duration is two takes → kept; same slot AND
    # duration is a duplicate; a different slot never matches.
    tag = lambda n, ln: {"isrc": "", "mb_trackid": "", "title": "Song",
                         "discnumber": 1, "tracknumber": n, "length": ln, "size": 0}
    assert match_sibling_track(tag(3, 200.0), [tag(3, 260.0)]) is None
    assert match_sibling_track(tag(3, 200.0), [tag(3, 200.5)]) is not None
    assert match_sibling_track(tag(3, 200.0), [tag(4, 200.0)]) is None


def test_find_sibling_album_dirs_does_not_group_distinct_years(tmp_path, monkeypatch):
    # Two live albums recorded on different dates share a name but are different
    # works — consolidation deletes "duplicate" tracks, so they must NOT be
    # grouped. A one-sided year (the same release re-tagged) still groups.
    from qobuz_librarian.modes import consolidate as c
    artist = tmp_path / "Queen"
    primary = artist / "Live at Wembley 1990"
    other = artist / "Live at Wembley 1992"
    same = artist / "Live at Wembley"
    for d in (primary, other, same):
        d.mkdir(parents=True)
    album = {"title": "Live at Wembley 1990"}
    sibs = {d.name for d, _ in c.find_sibling_album_dirs(album, primary)}
    assert "Live at Wembley 1992" not in sibs   # distinct year → not grouped
    assert "Live at Wembley" in sibs            # one-sided year → still grouped


def test_execute_consolidation_moves_overlap_to_recoverable_backup(tmp_path, monkeypatch):
    # Consolidation now MOVES overlapping sibling tracks to the gap-fill backup
    # dir (recoverable by the retention sweep) instead of hard-deleting them, so a
    # mistaken duplicate-match can be undone like every other destructive mode.
    import qobuz_librarian.config as cfg
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    sib = tmp_path / "Album (Deluxe)"
    sib.mkdir()
    f1 = sib / "track.flac"
    f1.write_bytes(b"audio-1")
    f2 = sib / "other.flac"
    f2.write_bytes(b"audio-2")
    summary = {"dir": str(sib),
               "overlap": [({"path": str(f1)}, {}), ({"path": str(f2)}, {})],
               "unique": []}

    removed, n_fail = execute_consolidation(summary)

    # Reported for the beets-DB drop, gone from the live folder, and recoverable
    # under the backup dir — not destroyed.
    assert n_fail == 0
    assert sorted(p.name for p in removed) == ["other.flac", "track.flac"]
    assert not f1.exists() and not f2.exists()
    recovered = {p.name for p in (tmp_path / "backups").rglob("*.flac")}
    assert recovered == {"track.flac", "other.flac"}


def test_consolidate_albums_is_a_noop_under_dry_run(monkeypatch):
    # Consolidation deletes files, so under --dry-run it must stop before it even
    # looks for the album on disk — the "already complete" album path reaches it
    # ahead of process_album's own dry-run stop. consolidate_albums imports
    # find_album_dir_filesystem locally from catalog, so patch it there: the
    # guard must return before that lookup is reached.
    from argparse import Namespace

    import qobuz_librarian.library.catalog as catmod
    import qobuz_librarian.modes.consolidate as cmod

    def _boom(*a, **k):
        raise AssertionError("dry-run consolidation must not look up or touch files")
    monkeypatch.setattr(catmod, "find_album_dir_filesystem", _boom)

    album = {"id": "x", "title": "Revolver", "artist": {"name": "The Beatles"}}
    assert cmod.consolidate_albums(album, Namespace(dry_run=True, consolidate=True,
                                                    yes=False)) == 0
