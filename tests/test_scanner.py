"""Tests for qobuz_librarian.library.scanner."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qobuz_librarian.library.scanner import (
    _list_artist_subdirs_cached,
    clear_scan_caches,
    list_artist_album_dirs,
    list_library_artists,
    parse_track_num,
    read_album_dir,
    read_flac_meta,
)


class TestParseTrackNum:
    def test_plain_integer(self):
        assert parse_track_num("5") == 5

    def test_slash_format(self):
        assert parse_track_num("1/12") == 1

    def test_unparseable(self):
        assert parse_track_num("abc") == 0


class TestListLibraryArtists:
    def _seed(self, tmp_path, name):
        """Create an artist dir with at least one audio file so the empty-
        dir skip in list_library_artists doesn't drop it."""
        d = tmp_path / name / "Album"
        d.mkdir(parents=True)
        (d / "01.flac").write_bytes(b"audio")
        return tmp_path / name

    def test_returns_artist_dirs(self, tmp_path):
        self._seed(tmp_path, "Pink Floyd")
        self._seed(tmp_path, "Radiohead")
        with patch("qobuz_librarian.config.MUSIC_ROOT", tmp_path), \
             patch("qobuz_librarian.config.STAGING_DIR", tmp_path / ".staging"):
            result = list_library_artists()
        names = [d.name for d in result]
        assert "Pink Floyd" in names
        assert "Radiohead" in names

    def test_excludes_dot_folders(self, tmp_path):
        self._seed(tmp_path, "Pink Floyd")
        (tmp_path / ".AppleDouble").mkdir()
        with patch("qobuz_librarian.config.MUSIC_ROOT", tmp_path), \
             patch("qobuz_librarian.config.STAGING_DIR", tmp_path / ".staging"):
            result = list_library_artists()
        names = [d.name for d in result]
        assert ".AppleDouble" not in names
        assert "Pink Floyd" in names

    def test_returns_empty_when_music_root_missing(self, tmp_path):
        with patch("qobuz_librarian.config.MUSIC_ROOT", tmp_path / "nonexistent"):
            result = list_library_artists()
        assert result == []

    def test_skips_empty_artist_dirs(self, tmp_path):
        (tmp_path / "Empty Artist").mkdir()
        real = tmp_path / "Real Artist" / "Album"
        real.mkdir(parents=True)
        (real / "01 - Track.flac").write_bytes(b"audio")
        with patch("qobuz_librarian.config.MUSIC_ROOT", tmp_path), \
             patch("qobuz_librarian.config.STAGING_DIR", tmp_path / ".staging"):
            names = [d.name for d in list_library_artists()]
        assert names == ["Real Artist"]

    def test_skips_artist_dir_with_only_cover_jpg(self, tmp_path):
        cover_only = tmp_path / "Cover Only Artist"
        cover_only.mkdir()
        (cover_only / "cover.jpg").write_bytes(b"img")
        real = tmp_path / "Real Artist" / "Album"
        real.mkdir(parents=True)
        (real / "01.flac").write_bytes(b"audio")
        with patch("qobuz_librarian.config.MUSIC_ROOT", tmp_path), \
             patch("qobuz_librarian.config.STAGING_DIR", tmp_path / ".staging"):
            names = [d.name for d in list_library_artists()]
        assert names == ["Real Artist"]


class TestListArtistAlbumDirs:
    def test_returns_album_dirs(self, tmp_path):
        (tmp_path / "OK Computer (1997)").mkdir()
        (tmp_path / "Kid A (2000)").mkdir()
        result = list_artist_album_dirs(tmp_path)
        names = [d.name for d in result]
        assert "OK Computer (1997)" in names

    def test_excludes_dot_folders(self, tmp_path):
        (tmp_path / "OK Computer (1997)").mkdir()
        (tmp_path / ".DS_Store_dir").mkdir()
        result = list_artist_album_dirs(tmp_path)
        names = [d.name for d in result]
        assert ".DS_Store_dir" not in names


class TestReadAlbumDir:
    def test_finds_flac_files(self, tmp_path):
        (tmp_path / "01 - Track One.flac").write_bytes(b"")
        (tmp_path / "02 - Track Two.flac").write_bytes(b"")
        with patch("qobuz_librarian.library.scanner.HAVE_MUTAGEN", False):
            result = read_album_dir(tmp_path)
        assert len(result) == 2

    def test_skips_non_audio_files(self, tmp_path):
        (tmp_path / "cover.jpg").write_bytes(b"")
        (tmp_path / "01 - Track.flac").write_bytes(b"")
        with patch("qobuz_librarian.library.scanner.HAVE_MUTAGEN", False):
            result = read_album_dir(tmp_path)
        assert len(result) == 1

    def test_filename_fallback_parses_track_number(self, tmp_path):
        (tmp_path / "05 - My Song.flac").write_bytes(b"")
        with patch("qobuz_librarian.library.scanner.HAVE_MUTAGEN", False):
            result = read_album_dir(tmp_path)
        assert result[0]["tracknumber"] == 5
        assert result[0]["title"] == "My Song"

    def test_uses_mutagen_meta_when_available(self, tmp_path):
        flac = tmp_path / "01 - Track.flac"
        flac.write_bytes(b"")
        fake_meta = {
            "title": "Real Title", "tracknumber": 1, "discnumber": 1,
            "isrc": "USRC1234567", "mb_trackid": "", "album": "Real Album",
            "albumartist": "Artist", "bits": 24, "sample_rate": 96000,
            "length": 245.0, "path": str(flac),
        }
        with patch("qobuz_librarian.library.scanner.HAVE_MUTAGEN", True), \
             patch("qobuz_librarian.library.scanner.read_flac_meta",
                   return_value=fake_meta):
            result = read_album_dir(tmp_path)
        assert result[0]["title"] == "Real Title"
        assert result[0]["bits"] == 24

    def test_symlink_loop_does_not_recurse(self, tmp_path):
        """A symlink loop in the album dir must not crash or hang the scan."""
        album = tmp_path / "Album"
        album.mkdir()
        (album / "01 - Track.flac").write_bytes(b"")
        sub = album / "sub"
        sub.mkdir()
        (sub / "loop").symlink_to(album)
        with patch("qobuz_librarian.library.scanner.HAVE_MUTAGEN", False):
            result = read_album_dir(album)
        assert len(result) == 1
        assert result[0]["tracknumber"] == 1


def test_non_flac_track_takes_its_disc_from_the_parent_folder(tmp_path):
    from qobuz_librarian.library.scanner import read_album_dir
    disc2 = tmp_path / "Album" / "Disc 2"
    disc2.mkdir(parents=True)
    (disc2 / "03 - Song.mp3").write_bytes(b"")     # non-flac -> filename fallback
    tracks = read_album_dir(tmp_path / "Album")
    assert tracks and tracks[0]["discnumber"] == 2
    assert tracks[0]["tracknumber"] == 3


def test_flac_cache_hits_when_unchanged_and_invalidates_on_change(tmp_path, monkeypatch):
    import time

    import qobuz_librarian.config as cfg
    from qobuz_librarian.library import flac_cache
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "FLAC_CACHE_ENABLED", True)
    flac_cache._reset_for_tests()
    try:
        f = tmp_path / "song.flac"
        f.write_bytes(b"abc")
        assert flac_cache.get(f) is None                       # cold miss
        flac_cache.put(f, {"title": "T", "isrc": "X"})
        assert flac_cache.get(f) == {"title": "T", "isrc": "X"}  # hit, unchanged
        time.sleep(0.01)
        f.write_bytes(b"abcd")                                  # mtime + size change
        assert flac_cache.get(f) is None                       # self-invalidated
    finally:
        flac_cache._reset_for_tests()
