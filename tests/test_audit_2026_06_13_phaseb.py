"""Regression tests for the 2026-06-13 fresh-audit (Phase B) fixes.

Focused on the data-loss / silent-failure findings that are unit-testable.
"""


# ── consolidate: two sibling tracks may not both claim one primary track ────

def test_consolidation_summary_no_double_claim(tmp_path, monkeypatch):
    from qobuz_librarian.modes import consolidate
    primary = [{"title": "Intro", "tracknumber": 1}]
    sib_tracks = [{"path": "/a/1.flac", "title": "Intro"},
                  {"path": "/a/2.flac", "title": "Intro"}]
    monkeypatch.setattr(consolidate, "read_album_dir", lambda d: sib_tracks)
    # Force both sibling tracks to "match" the SAME primary track.
    monkeypatch.setattr(consolidate, "match_sibling_track", lambda st, pts: primary[0])
    summaries = consolidate.consolidation_summary([(tmp_path, 0.9)], primary)
    s = summaries[0]
    assert len(s["overlap"]) == 1   # only one is a real duplicate of the primary
    assert len(s["unique"]) == 1    # the other is preserved, not deleted


# ── upgrade verify: refuse to delete the backup on a quality downgrade ──────

def test_upgrade_verify_rejects_quality_downgrade(tmp_path, monkeypatch):
    from qobuz_librarian.modes import process
    post = tmp_path / "post"
    post.mkdir()
    backup = tmp_path / "backup"
    backup.mkdir()

    def read_lower_post(d):
        # Same track count + playtime on both sides; only quality differs.
        if "post" in str(d):
            return [{"bits": 16, "sample_rate": 44100, "length": 100, "path": f"{d}/1.flac"}]
        return [{"bits": 24, "sample_rate": 96000, "length": 100, "path": f"{d}/1.flac"}]

    monkeypatch.setattr(process, "read_album_dir", read_lower_post)
    monkeypatch.setattr(process, "find_album_dir_filesystem", lambda a: post)
    monkeypatch.setattr(process, "drop_artist_subdirs_cache", lambda p: None)
    # Qobuz under-delivered 16/44 where the original was 24/96 → keep the backup.
    assert process._upgrade_replacement_verified({}, post, backup) is False

    def read_equal(d):
        return [{"bits": 24, "sample_rate": 96000, "length": 100, "path": f"{d}/1.flac"}]

    monkeypatch.setattr(process, "read_album_dir", read_equal)
    # Genuine same-quality re-rip still verifies (we don't block legit upgrades).
    assert process._upgrade_replacement_verified({}, post, backup) is True


# ── backup sweep: distinguish a stranded mid-copy .partial from a committed
#    backup whose album name merely ends in '.partial' ──────────────────────

def test_partial_sidecar_committed_backup_is_preserved(tmp_path, monkeypatch):
    import qobuz_librarian.library.backup as bk
    monkeypatch.setattr(bk.cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(bk.cfg, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    (tmp_path / "backups").mkdir()

    # A genuine stranded mid-copy: no origin sidecar → reaped + not surfaced.
    stranded = tmp_path / "backups" / "20260101_120000_Album.partial"
    stranded.mkdir()
    (stranded / "01.flac").write_bytes(b"x" * 2000)

    # A committed backup whose album name ends in '.partial': HAS the sidecar →
    # must survive the sweep and still be surfaced as a sole copy.
    committed = tmp_path / "backups" / "20260101_130000_Greatest Hits.partial"
    committed.mkdir()
    (committed / "01.flac").write_bytes(b"y" * 2000)
    (committed / bk._ORIGIN_SIDECAR).write_text("/music/Greatest Hits.partial")

    surfaced = {e.name for e, _ in bk.find_only_copy_backups()}
    assert committed.name in surfaced
    assert stranded.name not in surfaced

    # Age the stranded dir past the grace window so the sweep treats it as a
    # dead mid-copy (a fresh one might be another process's live copy).
    import os
    import time
    old = time.time() - 7200
    os.utime(stranded, (old, old))
    bk.cleanup_old_upgrade_backups(force=True)
    assert not stranded.exists()      # stranded mid-copy reaped
    assert committed.exists()         # committed backup preserved
