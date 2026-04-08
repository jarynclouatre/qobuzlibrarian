"""Qobuz API session and the core ``qobuz_get`` request.

Shared exceptions and auth helpers live in api/auth.py; this module imports
from there and never the reverse, which keeps the dependency acyclic. The
search/lookup helpers built on ``qobuz_get`` live in api/search.py.
"""
import threading
import time

import requests

from qobuz_librarian import config
from qobuz_librarian.api.auth import AuthLost, QobuzError, notify_auth_state
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log, vlog


# ── Session ───────────────────────────────────────────────────────────────────
def _ua_string() -> str:
    try:
        from importlib.metadata import version as _pkg_version
        return f"qobuz-librarian/{_pkg_version('qobuz-librarian')} (+streamrip-companion)"
    except Exception:
        return "qobuz-librarian (+streamrip-companion)"


# One requests.Session per thread. The session only pools connections and
# carries the User-Agent — auth is passed per request (X-User-Auth-Token) — so
# there's no shared mutable state to protect. Giving each worker its own
# session lets the parallel artist scan make real concurrent calls instead of
# queuing behind one global lock, while keeping single-threaded callers
# unchanged. `_get_session` is the seam tests patch.
_thread_local = threading.local()


def _get_session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": _ua_string()})
        _thread_local.session = s
    return s

# Retry on transient failures (rate limit + 5xx). Three attempts, exponential
# backoff capped at 8s — long enough to outwait a typical Qobuz hiccup, short
# enough that a sustained outage still fails the call quickly. Honors a
# Retry-After header if Qobuz sends one. 401/403/404 do NOT retry.
#
# Per-attempt budget, derived from WEB_FETCH_TIMEOUT (rather than a second
# independently-tuned literal) so a single stalled request frees a web
# awaiter close to when it gives up. A call that retries can outlive that
# window, but only as a bounded background thread (≤_MAX_ATTEMPTS) the
# executor pool absorbs — the user already got their timeout response.
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
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            r = _get_session().get(url, params=params, headers=headers,
                                   timeout=_REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt == _MAX_ATTEMPTS:
                raise QobuzError(
                    f"{_net_reason(e)} (while calling {endpoint})") from e
            wait = min(2 ** (attempt - 1), 8)
            vlog(f"{endpoint}: network error ({e}); retry {attempt}/{_MAX_ATTEMPTS} in {wait}s")
            _retry_sleep(wait)
            continue
        if r.status_code == 401:
            notify_auth_state(False)
            raise AuthLost(f"401 from Qobuz {endpoint}")
        if r.status_code in _RETRY_STATUSES:
            if attempt < _MAX_ATTEMPTS:
                # `is not None`, not truthiness: a server "Retry-After: 0"
                # (retry immediately) is valid and must not fall back to backoff.
                _ra = _retry_after(r)
                wait = _ra if _ra is not None else min(2 ** (attempt - 1), 8)
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
            raise QobuzError(
                f"Qobuz API kept returning HTTP {r.status_code} after "
                f"{_MAX_ATTEMPTS} attempts (while calling {endpoint}) — "
                f"rate-limited or a temporary outage; try again later.")
        if r.status_code != 200:
            raise QobuzError(f"HTTP {r.status_code} from {endpoint}: {r.text[:200]}")
        # A 200 means Qobuz accepted the token. Report that so the web
        # dashboard's auth banner recovers from an earlier transient 401
        # instead of staying red until the next restart probe.
        notify_auth_state(True)
        try:
            return r.json()
        except ValueError as e:
            # requests raises its own JSONDecodeError (a ValueError subclass,
            # and simplejson's variant when that's installed) — catch the base
            # so a junk body surfaces as a QobuzError, not an opaque traceback.
            raise QobuzError(f"bad JSON from {endpoint}: {e}") from e
