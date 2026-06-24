"""Env-var validation: a bad value falls back loudly instead of surfacing
later as an opaque download/lyrics failure."""
import os

import qobuz_librarian.config as cfg


def test_env_choice_falls_back_on_unknown_value(monkeypatch):
    monkeypatch.setenv("LYRICS_FORMAT", "lrc")
    assert cfg._env_choice("LYRICS_FORMAT", "embed",
                           ("embed", "sidecar", "both")) == "embed"


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


def test_env_unit_float_clamps_out_of_range_thresholds(monkeypatch):
    # Fuzzy thresholds are 0–1 similarity scores and several gate destructive
    # review paths, so an out-of-range typo must clamp into range rather than
    # make consolidation match everything (<0). 0.0 is a legit boundary and
    # passes through; a non-numeric value falls back to the default.
    monkeypatch.setenv("CONSOLIDATE_THRESH", "-1")
    assert cfg._env_unit_float("CONSOLIDATE_THRESH", 0.70) == 0.0
    monkeypatch.setenv("CONSOLIDATE_THRESH", "1.5")
    assert cfg._env_unit_float("CONSOLIDATE_THRESH", 0.70) == 1.0
    monkeypatch.setenv("CONSOLIDATE_THRESH", "0")
    assert cfg._env_unit_float("CONSOLIDATE_THRESH", 0.70) == 0.0
    monkeypatch.setenv("CONSOLIDATE_THRESH", "loose")
    assert cfg._env_unit_float("CONSOLIDATE_THRESH", 0.70) == 0.70


def test_resolve_secret_reads_token_from_a_file(monkeypatch, tmp_path):
    # Docker-secret style: the token lives in a file, not the environment, so
    # it stays out of `docker inspect`. The trailing newline a file carries must
    # be stripped. The resolved value is NOT written back to os.environ — doing
    # so re-exported the secret into every subprocess the app spawns.
    # Empty (compose's `${VAR:-}`) means "unset" to the resolver.
    monkeypatch.setenv("QOBUZ_USER_AUTH_TOKEN", "")
    token_file = tmp_path / "qobuz_token"
    token_file.write_text("tok-from-file\n")
    monkeypatch.setenv("QOBUZ_USER_AUTH_TOKEN_FILE", str(token_file))
    assert cfg._resolve_secret("QOBUZ_USER_AUTH_TOKEN") == "tok-from-file"
    # Must NOT leak the secret into the process environment.
    assert os.environ.get("QOBUZ_USER_AUTH_TOKEN") == ""
