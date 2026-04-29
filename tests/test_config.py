"""Env-var validation: a bad value falls back loudly instead of surfacing
later as an opaque download/lyrics failure."""
import os

import qobuz_librarian.config as cfg


def test_env_choice_falls_back_on_unknown_value(monkeypatch):
    monkeypatch.setenv("LYRICS_FORMAT", "lrc")
    assert cfg._env_choice("LYRICS_FORMAT", "embed",
                           ("embed", "sidecar", "both")) == "embed"


def test_env_choice_accepts_known_value_case_insensitively(monkeypatch):
    monkeypatch.setenv("ARTWORK", "BOTH")
    assert cfg._env_choice("ARTWORK", "sidecar",
                           ("sidecar", "embed", "both")) == "both"


def test_env_bool_empty_string_means_unset(monkeypatch):
    # compose's `${PREFER_HIRES:-}` resolves to "" — that must mean "use the
    # default", not silently flip the flag off.
    monkeypatch.setenv("PREFER_HIRES", "")
    assert cfg._env_bool("PREFER_HIRES", True) is True


def test_env_num_min_floors_a_sub_minimum_count(monkeypatch):
    # A 0 or negative worker count would crash the thread-pool constructor at
    # import time; clamp to the floor so a typo can't take down the web app.
    monkeypatch.setenv("SSE_MAX_WORKERS", "0")
    assert cfg._env_num_min("SSE_MAX_WORKERS", 16, 1) == 1


def test_env_num_min_clamps_a_negative_delay_to_zero(monkeypatch):
    # A negative delay reaches time.sleep, which raises ValueError and kills the
    # worker thread mid-scan. Flooring at 0 keeps a fat-fingered value harmless.
    monkeypatch.setenv("ARTIST_API_DELAY", "-1")
    assert cfg._env_num_min("ARTIST_API_DELAY", 0.0, 0.0) == 0.0


def test_resolve_secret_reads_token_from_a_file(monkeypatch, tmp_path):
    # Docker-secret style: the token lives in a file, not the environment, so
    # it stays out of `docker inspect`. The trailing newline a file carries must
    # be stripped, and the value published back so direct os.environ readers agree.
    # Empty (compose's `${VAR:-}`) means "unset" to the resolver, and lets
    # monkeypatch own the key so its write-back doesn't leak into later tests.
    monkeypatch.setenv("QOBUZ_USER_AUTH_TOKEN", "")
    token_file = tmp_path / "qobuz_token"
    token_file.write_text("tok-from-file\n")
    monkeypatch.setenv("QOBUZ_USER_AUTH_TOKEN_FILE", str(token_file))
    assert cfg._resolve_secret("QOBUZ_USER_AUTH_TOKEN") == "tok-from-file"
    assert os.environ["QOBUZ_USER_AUTH_TOKEN"] == "tok-from-file"


def test_resolve_secret_prefers_a_set_env_var_over_the_file(monkeypatch, tmp_path):
    token_file = tmp_path / "qobuz_token"
    token_file.write_text("from-file")
    monkeypatch.setenv("QOBUZ_USER_AUTH_TOKEN", "from-env")
    monkeypatch.setenv("QOBUZ_USER_AUTH_TOKEN_FILE", str(token_file))
    assert cfg._resolve_secret("QOBUZ_USER_AUTH_TOKEN") == "from-env"
