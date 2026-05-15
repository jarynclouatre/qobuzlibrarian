"""Tests for qobuz_librarian.api.auth — pattern detection, token I/O."""
import tomllib
from unittest.mock import patch

import pytest

from qobuz_librarian.api.auth import (
    NoCredsError,
    QobuzError,
    detect_auth_lost,
    detect_disk_full,
    detect_rate_limited,
    friendly_qobuz_error,
    load_qobuz_token,
    sync_streamrip_creds_from_env,
    write_streamrip_creds,
)


def test_detect_auth_lost_only_fires_on_http_401():
    # Real auth-lost signal.
    assert detect_auth_lost("Error: http 401 from endpoint") is True
    # "401" appearing in track titles or counts must not trigger — that
    # would falsely tear down credentials mid-download.
    assert detect_auth_lost("Downloaded track 401 - Song Title.flac") is False
    assert detect_auth_lost("Downloading track 401 of 500") is False
    assert detect_auth_lost("") is False


def test_detect_rate_limited_catches_429_and_persistent_failures():
    assert detect_rate_limited("Error: HTTP 429 from endpoint") is True
    # Streamrip exhausting its retries reads as throttling.
    assert detect_rate_limited(
        "Persistent error downloading track 'X', skipping") is True
    # A single retry is normal — don't flag every transient hiccup.
    assert detect_rate_limited("Error downloading track 'X', retrying") is False


def test_detect_disk_full_catches_errno_28_and_quota():
    assert detect_disk_full("OSError: [Errno 28] No space left on device") is True
    assert detect_disk_full("rip aborted: disk quota exceeded") is True
    assert detect_disk_full("Downloaded 12 tracks") is False


def test_load_qobuz_token_happy_and_error_paths(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[qobuz]\nuse_auth_token = true\n'
        'email_or_userid = "12345"\n'
        'password_or_token = "mytoken"\n'
    )
    with patch("qobuz_librarian.config.STREAMRIP_CONFIG", cfg):
        assert load_qobuz_token() == ("12345", "mytoken")

    # Missing file, disabled flag, empty token, and garbage TOML all raise
    # NoCredsError so the caller can route the user to Settings.
    for content in (None,
                    '[qobuz]\nuse_auth_token = false\n',
                    '[qobuz]\nuse_auth_token = true\nemail_or_userid = "x"\n'
                    'password_or_token = ""\n',
                    "this is not toml ===]]] [[[ ==="):
        if content is None:
            cfg.unlink()
        else:
            cfg.write_text(content)
        with patch("qobuz_librarian.config.STREAMRIP_CONFIG", cfg):
            with pytest.raises(NoCredsError):
                load_qobuz_token()


def test_sync_streamrip_creds_from_env_writes_and_stays_idempotent(tmp_path, monkeypatch):
    from qobuz_librarian import config
    cfg_path = tmp_path / "streamrip" / "config.toml"
    monkeypatch.setattr(config, "QOBUZ_USER_AUTH_TOKEN", "tok-abc")
    monkeypatch.setattr(config, "QOBUZ_USER_ID", "42")
    monkeypatch.setattr(config, "STREAMRIP_CONFIG", cfg_path)
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "staging")

    assert sync_streamrip_creds_from_env() is True
    data = tomllib.loads(cfg_path.read_text())
    assert data["qobuz"]["password_or_token"] == "tok-abc"
    assert data["qobuz"]["email_or_userid"] == "42"
    assert data["qobuz"]["use_auth_token"] is True
    # Second call is a no-op when nothing changed.
    assert sync_streamrip_creds_from_env() is None
    # A token rotation must rewrite the file.
    monkeypatch.setattr(config, "QOBUZ_USER_AUTH_TOKEN", "tok-new")
    assert sync_streamrip_creds_from_env() is True
    assert tomllib.loads(cfg_path.read_text())["qobuz"]["password_or_token"] == "tok-new"


def test_write_streamrip_creds_writes_streamrip_2_2_schema(tmp_path, monkeypatch):
    from qobuz_librarian import config
    cfg_path = tmp_path / "sr" / "config.toml"
    monkeypatch.setattr(config, "STREAMRIP_CONFIG", cfg_path)
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "stg")
    assert write_streamrip_creds("uid", "tok") is True
    data = tomllib.loads(cfg_path.read_text())
    # Streamrip 2.2 expects a [qobuz.secrets] table and a misc.version field.
    assert "secrets" in data["qobuz"]
    assert "version" in data.get("misc", {})
    assert data["database"]["downloads_path"] == str(cfg_path.parent / "downloads.db")
    assert data["downloads"]["folder"] == str(tmp_path / "stg")

    # A write to a path that can't be created (parent is a file) fails clean.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(config, "STREAMRIP_CONFIG", blocker / "sub" / "c.toml")
    assert write_streamrip_creds("u", "t") is False


def test_friendly_qobuz_error_strips_response_body():
    # Raw JSON / HTML response bodies must not leak into user-facing errors.
    e = QobuzError('HTTP 404 from album/get: {"status":"error","code":404}')
    assert friendly_qobuz_error(e) == "HTTP 404 from album/get"
    e = QobuzError("HTTP 500 from artist/get: <html>\n  <body>...</body>\n</html>")
    assert friendly_qobuz_error(e) == "HTTP 500 from artist/get"
    # A malformed-200 body raises "bad JSON from …: <decode error>"; the raw
    # JSONDecodeError text mustn't reach the UI either.
    e = QobuzError("bad JSON from album/get: Expecting value: line 1 column 1 (char 0)")
    assert friendly_qobuz_error(e) == "bad JSON from album/get"
    # Non-HTTP messages pass through unchanged.
    assert friendly_qobuz_error(QobuzError("connection refused")) == "connection refused"
