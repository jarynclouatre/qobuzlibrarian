"""pytest shared fixtures and session-wide isolation.

Redirects DATA_DIR (and the file paths derived from it) into a temp dir
so a `pytest -q` run doesn't write a real lock file / lyric-state file /
fetch log under ``~/.local/share/qobuz-librarian/`` on the dev machine.

Individual tests still monkeypatch specific paths via ``tmp_path`` for
finer-grained control; this fixture only covers the global side effects
of importing the package and running the web lifespan in a TestClient.
"""
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_data_dir():
    """Point cfg.DATA_DIR and the files derived from it at a temp dir for
    the test session, so tests don't pollute the dev machine's HOME.

    Also default WEB_AUTH=none for the session so the web tests exercise the
    routes directly; the auth-specific tests flip it back on with monkeypatch,
    which restores this default on teardown.
    """
    from qobuz_librarian import config as cfg

    tmp_root = Path(tempfile.mkdtemp(prefix="qobuz-librarian-tests-"))
    cfg.DATA_DIR = tmp_root

    # Re-derive every path that was built off DATA_DIR at import time.
    cfg.FETCH_LOG_FILE       = tmp_root / ".qobuz_librarian_log.json"
    cfg.WALK_SEEN_FILE       = tmp_root / ".qobuz_walk_seen.txt"
    cfg.ALBUM_WALK_SEEN_FILE = tmp_root / ".qobuz_album_walk_seen.txt"
    cfg.PENDING_QUEUE_FILE   = tmp_root / ".qobuz_pending_queue.json"
    cfg.LYRIC_RETRY_FILE     = tmp_root / ".qobuz_lyric_retry.json"
    cfg.REPAIR_LOG_PATH      = tmp_root / ".qobuz_replaced_tracks.log"
    cfg.CAPPED_FILE          = tmp_root / ".qobuz_upgrade_capped.json"
    cfg.HIDDEN_FILE          = tmp_root / ".qobuz_hidden.json"
    cfg.LYRIC_FETCH_STATE_FILE = tmp_root / ".lyric_fetch_state.json"
    cfg.WEB_AUTH_FILE        = tmp_root / ".qobuz_web_auth.json"
    cfg.LOCK_FILE            = tmp_root / "qobuz_librarian.lock"
    # Keep the persistent caches out of the deterministic suite — tests that
    # mock qobuz_get / build fixture FLACs expect a fresh read each time. Each
    # cache's own test re-enables it.
    cfg.ALBUM_CACHE_ENABLED  = False
    cfg.FLAC_CACHE_ENABLED   = False
    # Suppress write-through job persistence for the deterministic suite — a
    # shared jobs.db would otherwise leak historical rows between tests. The
    # persistence-specific tests flip this off with monkeypatch and a reset.
    from qobuz_librarian.web import job_persistence
    job_persistence._disabled = True

    prior_web_auth = os.environ.get("WEB_AUTH")
    os.environ["WEB_AUTH"] = "none"

    yield

    if prior_web_auth is None:
        os.environ.pop("WEB_AUTH", None)
    else:
        os.environ["WEB_AUTH"] = prior_web_auth
    # Clean up the session tempdir, best-effort. The lock file may still
    # be held briefly by a TestClient lifespan that's tearing down;
    # ignore_errors=True keeps the test exit clean either way.
    import shutil
    shutil.rmtree(tmp_root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _fast_qobuz_retries(monkeypatch):
    """The client retries transient failures (429/5xx/network errors) with
    exponential backoff. In tests that's pure dead time — patch the
    indirection to a no-op so the suite stays fast. (Don't patch
    time.sleep globally; other modules sleep too.)"""
    monkeypatch.setattr("qobuz_librarian.api.client._retry_sleep", lambda *_: None)
