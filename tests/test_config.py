"""Env-var validation: a bad value falls back loudly instead of surfacing
later as an opaque download/lyrics failure."""
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


def test_env_int_min_floors_a_sub_minimum_count(monkeypatch):
    # A 0 or negative worker count would crash the thread-pool constructor at
    # import time; clamp to the floor so a typo can't take down the web app.
    monkeypatch.setenv("SSE_MAX_WORKERS", "0")
    assert cfg._env_int_min("SSE_MAX_WORKERS", 16, 1) == 1
