"""Tests for qobuz_fetch.api.auth"""
import re
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from qobuz_fetch.api.auth import (
    detect_auth_lost,
    detect_disk_full,
    detect_rate_limited,
    load_qobuz_token,
    sync_streamrip_creds_from_env,
    verify_streamrip_downloads_folder,
    write_streamrip_creds,
)


class TestDetectAuthLost:
    def test_http_401(self):
        assert detect_auth_lost("Error: http 401 from endpoint") is True

    def test_clean_output_false(self):
        assert detect_auth_lost("Downloaded track 401 - Song Title.flac") is False
        assert detect_auth_lost("") is False

    def test_track_number_not_matched(self):
        # bare "401" in a title must not trigger; pattern requires "http 401"
        assert detect_auth_lost("Downloading track 401 of 500") is False


class TestValidateTokenSurfacesNetworkErrors:
    def test_qobuz_error_logs_warning_and_does_not_exit(self, caplog):
        import logging

        from qobuz_fetch.api.auth import QobuzError
        from qobuz_fetch.api.client import validate_token

        def _net_err(*a, **k):
            raise QobuzError("connection refused")
        with patch("qobuz_fetch.api.client.qobuz_get", _net_err):
            with caplog.at_level(logging.INFO, logger="qobuz_librarian"):
                validate_token("tok")
        assert any("Couldn't reach Qobuz" in r.message for r in caplog.records)


class TestDetectDiskFull:
    def test_no_space_left(self):
        assert detect_disk_full("OSError: [Errno 28] No space left on device") is True

    def test_quota_exceeded(self):
        assert detect_disk_full("rip aborted: disk quota exceeded") is True

    def test_clean_output_false(self):
        assert detect_disk_full("Downloaded 12 tracks") is False
        assert detect_disk_full("") is False


class TestLoadQobuzToken:
    def test_returns_user_id_and_token(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[qobuz]\nuse_auth_token = true\n'
            'email_or_userid = "12345"\n'
            'password_or_token = "mytoken"\n'
        )
        with patch("qobuz_fetch.config.STREAMRIP_CONFIG", cfg_file):
            uid, tok = load_qobuz_token()
        assert uid == "12345"
        assert tok == "mytoken"

    def test_exits_when_config_missing(self, tmp_path):
        missing = tmp_path / "no_such_file.toml"
        with patch("qobuz_fetch.config.STREAMRIP_CONFIG", missing):
            from qobuz_fetch.api.auth import NoCredsError
            with pytest.raises(NoCredsError):
                load_qobuz_token()

    def test_exits_when_use_auth_token_false(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text('[qobuz]\nuse_auth_token = false\n')
        with patch("qobuz_fetch.config.STREAMRIP_CONFIG", cfg_file):
            from qobuz_fetch.api.auth import NoCredsError
            with pytest.raises(NoCredsError):
                load_qobuz_token()

    def test_exits_when_token_empty(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[qobuz]\nuse_auth_token = true\n'
            'email_or_userid = "12345"\n'
            'password_or_token = ""\n'
        )
        with patch("qobuz_fetch.config.STREAMRIP_CONFIG", cfg_file):
            from qobuz_fetch.api.auth import NoCredsError
            with pytest.raises(NoCredsError):
                load_qobuz_token()

    def test_exits_when_config_is_garbage_toml(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text("this is not toml ===]]] [[[ ===")
        with patch("qobuz_fetch.config.STREAMRIP_CONFIG", cfg_file):
            from qobuz_fetch.api.auth import NoCredsError
            with pytest.raises(NoCredsError, match="parse"):
                load_qobuz_token()


class TestSyncStreamripCredsFromEnv:
    def test_no_env_token_is_noop(self, tmp_path, monkeypatch):
        from qobuz_fetch import config
        monkeypatch.setattr(config, "QOBUZ_USER_AUTH_TOKEN", "")
        monkeypatch.setattr(config, "STREAMRIP_CONFIG", tmp_path / "c.toml")
        assert sync_streamrip_creds_from_env() is None
        assert not (tmp_path / "c.toml").exists()

    def test_env_token_written_then_idempotent(self, tmp_path, monkeypatch):
        from qobuz_fetch import config
        cfg_path = tmp_path / "streamrip" / "config.toml"
        monkeypatch.setattr(config, "QOBUZ_USER_AUTH_TOKEN", "tok-abc")
        monkeypatch.setattr(config, "QOBUZ_USER_ID", "42")
        monkeypatch.setattr(config, "STREAMRIP_CONFIG", cfg_path)
        monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "staging")

        assert sync_streamrip_creds_from_env() is True
        data = tomllib.load(open(cfg_path, "rb"))
        assert data["qobuz"]["password_or_token"] == "tok-abc"
        assert data["qobuz"]["email_or_userid"] == "42"
        assert data["qobuz"]["use_auth_token"] is True
        assert sync_streamrip_creds_from_env() is None

    def test_stale_token_is_rewritten(self, tmp_path, monkeypatch):
        from qobuz_fetch import config
        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(config, "QOBUZ_USER_AUTH_TOKEN", "old")
        monkeypatch.setattr(config, "QOBUZ_USER_ID", "u")
        monkeypatch.setattr(config, "STREAMRIP_CONFIG", cfg_path)
        monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "s")
        assert sync_streamrip_creds_from_env() is True
        monkeypatch.setattr(config, "QOBUZ_USER_AUTH_TOKEN", "new")
        assert sync_streamrip_creds_from_env() is True
        data = tomllib.load(open(cfg_path, "rb"))
        assert data["qobuz"]["password_or_token"] == "new"

    def test_write_failure_returns_false(self, tmp_path, monkeypatch):
        from qobuz_fetch import config
        monkeypatch.setattr(config, "QOBUZ_USER_AUTH_TOKEN", "t")
        monkeypatch.setattr(config, "QOBUZ_USER_ID", "u")
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        monkeypatch.setattr(config, "STREAMRIP_CONFIG", blocker / "sub" / "c.toml")
        assert write_streamrip_creds("u", "t") is False

    def test_written_config_is_streamrip_2_2_schema(self, tmp_path, monkeypatch):
        """The seeded config must be valid for streamrip 2.2.0: `secrets` key
        required, [misc].version present (suppresses migration prompt), db path
        adjacent to the config file (not hardcoded /config)."""
        from qobuz_fetch import config
        cfg_path = tmp_path / "sr" / "config.toml"
        monkeypatch.setattr(config, "STREAMRIP_CONFIG", cfg_path)
        monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "stg")
        assert write_streamrip_creds("uid", "tok") is True
        d = tomllib.load(open(cfg_path, "rb"))
        assert "secrets" in d["qobuz"]
        assert "version" in d.get("misc", {})
        assert d["database"]["downloads_path"] == str(cfg_path.parent / "downloads.db")
        assert d["downloads"]["folder"] == str(tmp_path / "stg")


class TestDetectRateLimited:
    def test_http_429(self):
        assert detect_rate_limited("Error: HTTP 429 from endpoint") is True

    def test_persistent_error_is_throttle(self):
        # streamrip exhausted its own retries — treat as throttle signal
        assert detect_rate_limited(
            "Persistent error downloading track 'X', skipping") is True

    def test_isolated_retry_is_not_throttle(self):
        # a single "retrying" is normal streamrip behaviour
        assert detect_rate_limited(
            "Error downloading track 'X', retrying") is False


# ── bundled streamrip-default.toml. ────────────────────────────────────
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
