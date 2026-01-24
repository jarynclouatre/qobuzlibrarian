"""Tests for files we ship inside the Docker image (docker/*).

These aren't tests of the auth module — they validate the bundled
streamrip default config against streamrip 2.2.0's actual format-key
expectations. A typo here means the first download silently lands in
the wrong folder layout.
"""
import re
import tomllib
from pathlib import Path

_PLACEHOLDER_RE = re.compile(r"\{(\w+)(?::[^}]*)?\}")

VALID_FOLDER_KEYS = {
    "albumartist", "albumcomposer", "bit_depth", "container", "id",
    "sampling_rate", "title", "year",
}
VALID_TRACK_KEYS = {
    "albumartist", "albumcomposer", "artist", "composer", "explicit",
    "id", "title", "tracknumber",
}

_DEFAULT_TOML = Path(__file__).resolve().parents[1] / "docker" / "streamrip-default.toml"


class TestStreamripDefaultToml:
    """folder_format and track_format must only reference placeholder keys that
    streamrip 2.2.0's format() info dict actually provides."""

    def _placeholders(self, fmt: str) -> set:
        return {m.group(1) for m in _PLACEHOLDER_RE.finditer(fmt)}

    def test_folder_format_keys(self):
        cfg = tomllib.load(open(_DEFAULT_TOML, "rb"))
        bad = self._placeholders(cfg["filepaths"]["folder_format"]) - VALID_FOLDER_KEYS
        assert not bad, f"folder_format uses unknown keys: {bad!r}"

    def test_track_format_keys(self):
        cfg = tomllib.load(open(_DEFAULT_TOML, "rb"))
        bad = self._placeholders(cfg["filepaths"]["track_format"]) - VALID_TRACK_KEYS
        assert not bad, f"track_format uses unknown keys: {bad!r}"

    def test_album_placeholder_not_used(self):
        # The exact regression: '{album}' instead of '{title}' in folder_format
        cfg = tomllib.load(open(_DEFAULT_TOML, "rb"))
        fmt = cfg["filepaths"]["folder_format"]
        assert "{album}" not in fmt and "{album:" not in fmt

    def test_downloads_db_disabled(self):
        # downloads.db dedupe is redundant with the librarian's own
        # compute_missing logic; leaving it on makes a re-download of
        # any track the user manually removed silently skip.
        cfg = tomllib.load(open(_DEFAULT_TOML, "rb"))
        assert cfg["database"]["downloads_enabled"] is False

    def test_add_singles_to_folder_enabled(self):
        # Per-track downloads (gap-fill walks) need their own folder so
        # beets routes them by tag, not by shared stage directory. With
        # this off, multi-album fills collapse into one on-disk folder.
        cfg = tomllib.load(open(_DEFAULT_TOML, "rb"))
        assert cfg["filepaths"]["add_singles_to_folder"] is True
