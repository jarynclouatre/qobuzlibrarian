"""Qobuz API session and core request.

validate_token() lives here (not in auth.py) because it calls qobuz_get()
and putting it here avoids a circular import: client.py imports AuthLost
from auth.py, so auth.py cannot import back from client.py.

Search helpers (search_albums, search_tracks, find_qobuz_track_by_isrc,
etc.) live in api/search.py.
"""
import json
import threading
import time

import requests

from qobuz_fetch import config
from qobuz_fetch.api.auth import AuthLost, QobuzError
from qobuz_fetch.ui_cli.colors import C, fmt
from qobuz_fetch.ui_cli.logging import log, vlog


# ── Session ───────────────────────────────────────────────────────────────────
def _ua_string() -> str:
    try:
        from importlib.metadata import version as _pkg_version
        return f"qobuz-librarian/{_pkg_version('qobuz-librarian')} (+streamrip-companion)"
    except Exception:
        return "qobuz-librarian (+streamrip-companion)"


_session = requests.Session()
_session.headers.update({"User-Agent": _ua_string()})
# requests.Session shares state (urllib3 pool, cookie jar, adapter caches)
# across threads. The web app fans calls out via run_in_executor + sync route
# handlers, so concurrent gets are real. Serialize the call to keep that
# shared state consistent; per-call QPS is low enough that this isn't felt.
_session_lock = threading.Lock()

# Retry on transient failures (rate limit + 5xx). Three attempts, exponential
# backoff capped at 8s — long enough to outwait a typical Qobuz hiccup, short
# enough that a sustained outage still fails the call quickly. Honors a
# Retry-After header if Qobuz sends one. 401/403/404 do NOT retry.
#
# _REQUEST_TIMEOUT must stay below WEB_FETCH_TIMEOUT so a cancelled
# asyncio.wait_for awaiter doesn't leak the executor thread for the full
# window. Derive from config rather than risking drift between two
# independently-tuned literals.
_REQUEST_TIMEOUT = max(2, int(config.WEB_FETCH_TIMEOUT) - 2)
_RETRY_STATUSES  = (429, 500, 502, 503, 504)
_MAX_ATTEMPTS    = 3


def _retry_after(resp) -> float | None:
    """Parse Retry-After header (seconds form only — Qobuz never sends an HTTP-date).
    Falls back to None when header is missing or malformed."""
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return min(max(float(val), 0.0), 30.0)
    except ValueError:
        return None


def _retry_sleep(seconds: float):
    """Indirection so tests can monkeypatch backoff to a no-op without
    affecting every other `time.sleep` in the codebase."""
    time.sleep(seconds)


# ── Core request ──────────────────────────────────────────────────────────────
def _net_reason(exc):
    """Short, human reason for a requests failure (not the urllib3 dump)."""
    if isinstance(exc, requests.Timeout):
        return "the Qobuz API timed out"
    if isinstance(exc, requests.ConnectionError):
        return "couldn't reach the Qobuz API (network down or blocked?)"
    if isinstance(exc, requests.TooManyRedirects):
        return "too many redirects from the Qobuz API"
    return "a network error reaching the Qobuz API"


def qobuz_get(endpoint, params, token):
    headers = {"X-App-Id": config.QOBUZ_APP_ID, "X-User-Auth-Token": token}
    url = f"{config.QOBUZ_API_BASE}/{endpoint}"
    last_err = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with _session_lock:
                r = _session.get(url, params=params, headers=headers,
                                 timeout=_REQUEST_TIMEOUT)
        except requests.RequestException as e:
            last_err = e
            if attempt == _MAX_ATTEMPTS:
                raise QobuzError(
                    f"{_net_reason(e)} (while calling {endpoint})") from e
            wait = min(2 ** (attempt - 1), 8)
            vlog(f"{endpoint}: network error ({e}); retry {attempt}/{_MAX_ATTEMPTS} in {wait}s")
            _retry_sleep(wait)
            continue
        if r.status_code == 401:
            raise AuthLost(f"401 from Qobuz {endpoint}")
        if r.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
            wait = _retry_after(r) or min(2 ** (attempt - 1), 8)
            if r.status_code == 429:
                # Surface rate-limit waits in the shared logger so the web
                # SSE stream shows "rate-limited, waiting Ns" instead of a
                # silent pause that looks like a hang.
                log.info(fmt(C.YELLOW,
                    f"  ⏳ Qobuz rate-limit — waiting {wait:.0f}s "
                    f"(retry {attempt}/{_MAX_ATTEMPTS})"))
            else:
                vlog(f"{endpoint}: HTTP {r.status_code}; retry "
                     f"{attempt}/{_MAX_ATTEMPTS} in {wait:.1f}s")
            _retry_sleep(wait)
            continue
        if r.status_code != 200:
            raise QobuzError(f"HTTP {r.status_code} from {endpoint}: {r.text[:200]}")
        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise QobuzError(f"bad JSON from {endpoint}: {e}") from e
    # All attempts exhausted on retryable failures.
    raise QobuzError(
        f"{_net_reason(last_err)} — still failing after {_MAX_ATTEMPTS} "
        f"attempts (while calling {endpoint})")


# ── Token preflight ───────────────────────────────────────────────────────────
def validate_token(token):
    """Lightweight preflight: surface an expired token before the user picks an album.

    Uses a cheap search so the round-trip is fast. Non-auth API errors are
    ignored — they don't mean the token is bad.
    """
    try:
        qobuz_get("album/search", {"query": "test", "limit": 1}, token)
    except AuthLost:
        from qobuz_fetch.ui_cli.errors import EXIT_AUTH, die
        die(fmt(C.RED,
            "\n✗  Qobuz token is expired or invalid."
            " Re-authenticate: Settings page in the web UI, or set QOBUZ_USER_AUTH_TOKEN env var.\n"),
            EXIT_AUTH)
    except QobuzError as e:
        # Don't treat a failed preflight as fatal — but tell the user so
        # later "search failed" errors don't look like the token is broken.
        log.info(fmt(C.YELLOW,
            f"  ⚠  Couldn't reach Qobuz on preflight ({e}); continuing."))
