"""Tests for qobuz_librarian.library.scanner — library walking, album reads,
and the FLAC tag cache."""
from unittest.mock import patch

from qobuz_librarian.library.scanner import (
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


def test_read_album_dir_strips_dot_and_space_track_prefixes(tmp_path):
    # mutagen can't read these, so the filename is the only title source. Legacy
    # "NN. Title" / "NN Title" rips must have the leading track number stripped
    # like "NN - Title" does, or the digits stay in the title and the track reads
    # as missing (spurious re-download) while the file reads as an extra.
    (tmp_path / "03. Dotted Song.flac").write_bytes(b"")
    (tmp_path / "04 Spaced Song.flac").write_bytes(b"")
    (tmp_path / "05.Glued Song.flac").write_bytes(b"")
    with patch("qobuz_librarian.library.scanner.HAVE_MUTAGEN", False):
        by_title = {t["title"]: t["tracknumber"] for t in read_album_dir(tmp_path)}
    assert by_title.get("Dotted Song") == 3
    assert by_title.get("Spaced Song") == 4
    assert by_title.get("Glued Song") == 5


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


def test_scan_checkpoint_load_coerces_wrong_types(tmp_path, monkeypatch):
    # A hand-edited / partially-written checkpoint with wrong-typed fields must
    # coerce back to []/[]/{} so the consumer's set()/dict() can't crash on resume.
    import json

    import qobuz_librarian.config as cfg
    from qobuz_librarian.library import scan_checkpoint

    cpfile = tmp_path / "checkpoint.json"
    monkeypatch.setattr(cfg, "SCAN_CHECKPOINT_FILE", cpfile)
    cpfile.write_text(json.dumps({
        "library": {"scanned": "oops", "candidates": {"not": "a list"}, "seen": ["x"]}
    }), encoding="utf-8")

    cp = scan_checkpoint.load("library")
    assert cp["scanned"] == [] and cp["candidates"] == [] and cp["seen"] == {}
    # The coerced shapes are safe for the consumer's set()/dict().
    assert set(cp["scanned"]) == set() and dict(cp["seen"]) == {}


def test_dir_caches_survive_a_concurrent_clear(monkeypatch, tmp_path):
    # clear_scan_caches() (a concurrent download) can empty a scan cache between
    # the `in` check and the lookup; that KeyError used to escape the OSError
    # guard and silently drop the artist. Both cached lookups must be atomic.
    from qobuz_librarian.library import scanner

    class _LyingCache(dict):
        def __contains__(self, k):   # present for the check, absent at the lookup
            return True

    artist = tmp_path / "Artist"
    (artist / "Album").mkdir(parents=True)
    monkeypatch.setattr(scanner, "_HAS_AUDIO_CACHE", _LyingCache())
    monkeypatch.setattr(scanner, "_ARTIST_SUBDIRS_CACHE", _LyingCache())
    assert scanner._has_audio_anywhere(artist) is False    # no KeyError
    assert [d.name for d in scanner._list_artist_subdirs_cached(artist)] == ["Album"]
