"""Tests for files we ship inside the Docker image (docker/*).

These aren't tests of the auth module — they validate the bundled
streamrip default config against streamrip 2.2.0's actual format-key
expectations. A typo here means the first download silently lands in
the wrong folder layout.
"""
import os
import re
import subprocess
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


_ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"


class TestEntrypointMigrations:
    """The entrypoint enforces librarian-required streamrip settings on every
    container start, not just the first-run seed. The /config volume persists
    across image rebuilds, so a config written by an older build must be
    brought into line rather than left stale."""

    def _run_head(self, tmp_path, env_extra):
        """Run entrypoint.sh up to (but not including) the dispatch case."""
        head, _, _ = _ENTRYPOINT.read_text().partition("# ── Dispatch")
        env = {**os.environ, **env_extra}
        subprocess.run(
            ["bash", "-c", head + "\nexit 0\n"],
            env=env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def test_downloads_enabled_flipped_on_existing_config(self, tmp_path):
        config_dir = tmp_path / "config"
        streamrip_dir = config_dir / "streamrip"
        beets_dir = config_dir / "beets"
        streamrip_dir.mkdir(parents=True)
        beets_dir.mkdir(parents=True)
        (beets_dir / "config.yaml").write_text("# placeholder\n")
        (streamrip_dir / "config.toml").write_text(
            "[database]\n"
            "downloads_enabled = true\n"
            "downloads_path = \"/config/streamrip/downloads.db\"\n"
            "failed_downloads_enabled = true\n"
        )

        self._run_head(tmp_path, {"CONFIG_DIR": str(config_dir)})

        result = (streamrip_dir / "config.toml").read_text()
        assert "downloads_enabled = false" in result
        assert "\ndownloads_enabled = true" not in result
        assert "failed_downloads_enabled = true" in result

    def test_non_numeric_puid_warns_and_stays_root(self, tmp_path):
        config_dir = tmp_path / "config"
        (config_dir / "streamrip").mkdir(parents=True)
        (config_dir / "beets").mkdir(parents=True)
        (config_dir / "beets" / "config.yaml").write_text("# placeholder\n")
        (config_dir / "streamrip" / "config.toml").write_text("[database]\n")
        head, _, _ = _ENTRYPOINT.read_text().partition("# ── Dispatch")
        env = {**os.environ, "CONFIG_DIR": str(config_dir),
               "PUID": "appuser", "PGID": "1000"}
        r = subprocess.run(["bash", "-c", head + "\nexit 0\n"], env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0
        assert "must be numeric" in r.stderr

    def test_add_singles_to_folder_flipped_on_existing_config(self, tmp_path):
        config_dir = tmp_path / "config"
        streamrip_dir = config_dir / "streamrip"
        beets_dir = config_dir / "beets"
        streamrip_dir.mkdir(parents=True)
        beets_dir.mkdir(parents=True)
        (beets_dir / "config.yaml").write_text("# placeholder\n")
        (streamrip_dir / "config.toml").write_text(
            "[filepaths]\n"
            "add_singles_to_folder = false\n"
            'folder_format = "{albumartist}/{title} ({year})"\n'
        )

        self._run_head(tmp_path, {"CONFIG_DIR": str(config_dir)})

        result = (streamrip_dir / "config.toml").read_text()
        assert "add_singles_to_folder = true" in result
        assert "add_singles_to_folder = false" not in result

    def test_already_correct_left_alone(self, tmp_path):
        config_dir = tmp_path / "config"
        streamrip_dir = config_dir / "streamrip"
        beets_dir = config_dir / "beets"
        streamrip_dir.mkdir(parents=True)
        beets_dir.mkdir(parents=True)
        (beets_dir / "config.yaml").write_text("# placeholder\n")
        original = (
            "[database]\n"
            "downloads_enabled = false\n"
            "failed_downloads_enabled = true\n"
            "[filepaths]\n"
            "add_singles_to_folder = true\n"
        )
        (streamrip_dir / "config.toml").write_text(original)

        self._run_head(tmp_path, {"CONFIG_DIR": str(config_dir)})

        assert (streamrip_dir / "config.toml").read_text() == original
