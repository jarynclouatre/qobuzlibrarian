"""Tests for qobuz_fetch.library.scanner."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qobuz_fetch.library.scanner import (
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
        with patch("qobuz_fetch.config.MUSIC_ROOT", tmp_path), \
             patch("qobuz_fetch.config.STAGING_DIR", tmp_path / ".staging"):
            result = list_library_artists()
        names = [d.name for d in result]
        assert "Pink Floyd" in names
        assert "Radiohead" in names

    def test_excludes_dot_folders(self, tmp_path):
        self._seed(tmp_path, "Pink Floyd")
        (tmp_path / ".AppleDouble").mkdir()
        with patch("qobuz_fetch.config.MUSIC_ROOT", tmp_path), \
             patch("qobuz_fetch.config.STAGING_DIR", tmp_path / ".staging"):
            result = list_library_artists()
        names = [d.name for d in result]
        assert ".AppleDouble" not in names
        assert "Pink Floyd" in names

    def test_returns_empty_when_music_root_missing(self, tmp_path):
        with patch("qobuz_fetch.config.MUSIC_ROOT", tmp_path / "nonexistent"):
            result = list_library_artists()
        assert result == []

    def test_skips_empty_artist_dirs(self, tmp_path):
        """Artist directories with no audio files anywhere in their tree
        should not be returned — they cost an API round-trip per walk and
        produce confusing '(0 albums)' output for the user."""
        (tmp_path / "Empty Artist").mkdir()
        real = tmp_path / "Real Artist" / "Album"
        real.mkdir(parents=True)
        (real / "01 - Track.flac").write_bytes(b"audio")
        with patch("qobuz_fetch.config.MUSIC_ROOT", tmp_path), \
             patch("qobuz_fetch.config.STAGING_DIR", tmp_path / ".staging"):
            names = [d.name for d in list_library_artists()]
        assert names == ["Real Artist"]

    def test_skips_artist_dir_with_only_cover_jpg(self, tmp_path):
        """An artist dir whose only content is a cover image (no audio
        anywhere) is still empty for library purposes — skip it."""
        cover_only = tmp_path / "Cover Only Artist"
        cover_only.mkdir()
        (cover_only / "cover.jpg").write_bytes(b"img")
        real = tmp_path / "Real Artist" / "Album"
        real.mkdir(parents=True)
        (real / "01.flac").write_bytes(b"audio")
        with patch("qobuz_fetch.config.MUSIC_ROOT", tmp_path), \
             patch("qobuz_fetch.config.STAGING_DIR", tmp_path / ".staging"):
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
        with patch("qobuz_fetch.library.scanner.HAVE_MUTAGEN", False):
            result = read_album_dir(tmp_path)
        assert len(result) == 2

    def test_skips_non_audio_files(self, tmp_path):
        (tmp_path / "cover.jpg").write_bytes(b"")
        (tmp_path / "01 - Track.flac").write_bytes(b"")
        with patch("qobuz_fetch.library.scanner.HAVE_MUTAGEN", False):
            result = read_album_dir(tmp_path)
        assert len(result) == 1

    def test_filename_fallback_parses_track_number(self, tmp_path):
        (tmp_path / "05 - My Song.flac").write_bytes(b"")
        with patch("qobuz_fetch.library.scanner.HAVE_MUTAGEN", False):
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
        with patch("qobuz_fetch.library.scanner.HAVE_MUTAGEN", True), \
             patch("qobuz_fetch.library.scanner.read_flac_meta",
                   return_value=fake_meta):
            result = read_album_dir(tmp_path)
        assert result[0]["title"] == "Real Title"
        assert result[0]["bits"] == 24

    def test_symlink_loop_does_not_recurse(self, tmp_path):
        """A symlink loop in the album dir must not crash or hang the scan.
        Path.rglob follows symlinks by default and goes into unbounded
        recursion; the helper must walk the loop entry as a leaf and return."""
        album = tmp_path / "Album"
        album.mkdir()
        (album / "01 - Track.flac").write_bytes(b"")
        sub = album / "sub"
        sub.mkdir()
        (sub / "loop").symlink_to(album)
        with patch("qobuz_fetch.library.scanner.HAVE_MUTAGEN", False):
            result = read_album_dir(album)
        assert len(result) == 1
        assert result[0]["tracknumber"] == 1
