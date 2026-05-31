"""Tests for qobuz_librarian.library.scanner — library walking, album reads,
and the FLAC tag cache."""
from unittest.mock import patch

from qobuz_librarian.library.scanner import (
    list_artist_album_dirs,
    list_library_artists,
    parse_track_num,
    read_album_dir,
)


def test_parse_track_num():
    assert parse_track_num("5") == 5
    assert parse_track_num("1/12") == 1
    assert parse_track_num("abc") == 0


def _seed_artist(root, name):
    d = root / name / "Album"
    d.mkdir(parents=True)
    (d / "01.flac").write_bytes(b"audio")
    return root / name


def test_list_library_artists_filters_dot_empty_and_art_only(tmp_path):
    _seed_artist(tmp_path, "Pink Floyd")
    _seed_artist(tmp_path, "Radiohead")
    (tmp_path / ".AppleDouble").mkdir()           # dot folder → excluded
    (tmp_path / "Empty Artist").mkdir()           # no audio → skipped
    cover_only = tmp_path / "Cover Only"
    cover_only.mkdir()
    (cover_only / "cover.jpg").write_bytes(b"img")  # art only → skipped
    with patch("qobuz_librarian.config.MUSIC_ROOT", tmp_path), \
         patch("qobuz_librarian.config.STAGING_DIR", tmp_path / ".staging"):
        names = sorted(d.name for d in list_library_artists())
    assert names == ["Pink Floyd", "Radiohead"]


def test_list_library_artists_empty_when_root_missing(tmp_path):
    with patch("qobuz_librarian.config.MUSIC_ROOT", tmp_path / "nonexistent"):
        assert list_library_artists() == []


def test_list_artist_album_dirs_excludes_dot_and_empty_folders(tmp_path):
    for album in ("OK Computer (1997)", "Kid A (2000)"):
        d = tmp_path / album
        d.mkdir()
        (d / "01.flac").write_bytes(b"audio")
    (tmp_path / ".DS_Store_dir").mkdir()
    (tmp_path / "Abandoned Download").mkdir()              # no audio → skipped
    art_only = tmp_path / "Art Only"
    art_only.mkdir()
    (art_only / "cover.jpg").write_bytes(b"img")           # art only → skipped
    trash = tmp_path / "Kid A (2000).restore_trash"        # interrupted restore
    trash.mkdir()
    (trash / "01.flac").write_bytes(b"audio")              # has audio, still skipped
    names = [d.name for d in list_artist_album_dirs(tmp_path)]
    assert names == ["Kid A (2000)", "OK Computer (1997)"]


def test_read_album_dir_filename_fallback_and_mutagen_meta(tmp_path):
    (tmp_path / "cover.jpg").write_bytes(b"")          # non-audio → skipped
    (tmp_path / "05 - My Song.flac").write_bytes(b"")
    # Without mutagen, fall back to parsing track/title from the filename.
    with patch("qobuz_librarian.library.scanner.HAVE_MUTAGEN", False):
        result = read_album_dir(tmp_path)
    assert len(result) == 1
    assert result[0]["tracknumber"] == 5 and result[0]["title"] == "My Song"

    # With mutagen, the real tag metadata is used.
    fake_meta = {"title": "Real Title", "tracknumber": 1, "discnumber": 1,
                 "isrc": "USRC1234567", "mb_trackid": "", "album": "Real Album",
                 "albumartist": "Artist", "bits": 24, "sample_rate": 96000,
                 "length": 245.0, "path": str(tmp_path / "05 - My Song.flac")}
    with patch("qobuz_librarian.library.scanner.HAVE_MUTAGEN", True), \
         patch("qobuz_librarian.library.scanner.read_audio_meta", return_value=fake_meta):
        result = read_album_dir(tmp_path)
    assert result[0]["title"] == "Real Title" and result[0]["bits"] == 24


def test_read_album_dir_survives_a_symlink_loop(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    (album / "01 - Track.flac").write_bytes(b"")
    sub = album / "sub"
    sub.mkdir()
    (sub / "loop").symlink_to(album)
    with patch("qobuz_librarian.library.scanner.HAVE_MUTAGEN", False):
        result = read_album_dir(album)
    assert len(result) == 1 and result[0]["tracknumber"] == 1


def test_non_flac_track_takes_disc_from_parent_folder(tmp_path):
    disc2 = tmp_path / "Album" / "Disc 2"
    disc2.mkdir(parents=True)
    (disc2 / "03 - Song.mp3").write_bytes(b"")   # non-flac → filename + folder fallback
    tracks = read_album_dir(tmp_path / "Album")
    assert tracks and tracks[0]["discnumber"] == 2 and tracks[0]["tracknumber"] == 3


def test_flac_cache_hits_when_unchanged_and_invalidates_on_change(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.library import flac_cache
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "FLAC_CACHE_ENABLED", True)
    flac_cache._reset_for_tests()
    try:
        f = tmp_path / "song.flac"
        f.write_bytes(b"abc")
        assert flac_cache.get(f) is None                        # cold miss
        flac_cache.put(f, {"title": "T", "isrc": "X"})
        assert flac_cache.get(f) == {"title": "T", "isrc": "X"}  # hit, unchanged
        f.write_bytes(b"abcd")                                   # size change invalidates
        assert flac_cache.get(f) is None                        # self-invalidated
    finally:
        flac_cache._reset_for_tests()


def test_flac_cache_stores_the_pre_read_signature(tmp_path, monkeypatch):
    # The signature is captured before the file is parsed; if the file changes
    # during that read, storing the prior signature makes the now-edited file
    # miss on the next lookup rather than serving the pre-edit tags.
    import qobuz_librarian.config as cfg
    from qobuz_librarian.library import flac_cache
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "FLAC_CACHE_ENABLED", True)
    flac_cache._reset_for_tests()
    try:
        f = tmp_path / "song.flac"
        f.write_bytes(b"original")
        sig = flac_cache.signature(f)
        f.write_bytes(b"edited while being read")
        flac_cache.put(f, {"title": "stale"}, sig=sig)
        assert flac_cache.get(f) is None
    finally:
        flac_cache._reset_for_tests()


def test_flac_cache_prune_drops_moved_rows_but_spares_unmounted_volume(tmp_path, monkeypatch):
    import qobuz_librarian.config as cfg
    from qobuz_librarian.library import flac_cache
    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "MUSIC_ROOT", music)
    monkeypatch.setattr(cfg, "FLAC_CACHE_ENABLED", True)
    flac_cache._reset_for_tests()
    try:
        here = music / "here.flac"
        here.write_bytes(b"x")
        gone = music / "gone.flac"
        gone.write_bytes(b"y")
        flac_cache.put(here, {"t": 1})
        flac_cache.put(gone, {"t": 2})

        gone.unlink()                                        # moved/deleted on disk
        assert flac_cache.prune_missing(force=True) == 1     # only the orphan goes
        assert flac_cache.get(here) == {"t": 1}              # live row untouched

        # Library volume unmounted: every path looks gone, but a prune must
        # not wipe the cache — those rows are still valid once it's back.
        monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "unmounted")
        assert flac_cache.prune_missing(force=True) == 0
        assert flac_cache.get(here) == {"t": 1}
    finally:
        flac_cache._reset_for_tests()


def test_flac_cache_put_buffers_and_flush_pending_drains_to_disk(tmp_path, monkeypatch):
    # A cold scan would commit per file otherwise; put() now buffers and
    # flush_pending writes the batch in one transaction. get() must still see
    # buffered rows (the put → get visibility contract scans rely on), and
    # opening a fresh sqlite connection must NOT see them until the flush.
    import sqlite3

    import qobuz_librarian.config as cfg
    from qobuz_librarian.library import flac_cache

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "FLAC_CACHE_ENABLED", True)
    flac_cache._reset_for_tests()
    try:
        f = tmp_path / "song.flac"
        f.write_bytes(b"abc")
        flac_cache.put(f, {"title": "T", "isrc": "X"})

        # The buffered row is visible via get() …
        assert flac_cache.get(f) == {"title": "T", "isrc": "X"}

        # … but a brand-new sqlite connection (the worker's per-thread conn
        # was never created since we didn't trigger a flush) sees an empty
        # table — the bytes haven't been committed yet.
        with sqlite3.connect(str(tmp_path / "flac_cache.db")) as side:
            cnt = side.execute("SELECT count(*) FROM files").fetchone()[0]
        assert cnt == 0, "put() should not have written to disk yet"

        # Draining the buffer commits the row.
        flac_cache.flush_pending()
        with sqlite3.connect(str(tmp_path / "flac_cache.db")) as side:
            cnt = side.execute("SELECT count(*) FROM files").fetchone()[0]
        assert cnt == 1

        # Idempotent — a second flush with an empty buffer is a no-op.
        flac_cache.flush_pending()
    finally:
        flac_cache._reset_for_tests()
