"""Optional single-user login for the web UI.

One username + password, opt-out via WEB_AUTH=none. Follows web/csrf.py's
cookie conventions (HttpOnly/SameSite, secrets.compare_digest) and persists
the credential the way the streamrip token is persisted — an atomic 0600
file in DATA_DIR.

The session cookie carries a per-credential secret rather than a per-login
token, so a browser stays signed in across container restarts and resetting
the password (which mints a new secret) invalidates every old cookie.
"""
import hashlib
import json
import os
import secrets
import tempfile

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response

from qobuz_librarian import config as cfg

SESSION_COOKIE = "qf_session"
LOGIN_PATH = "/login"
SETUP_PATH = "/setup"
MIN_PASSWORD_LEN = 8

# Reachable without a session: the auth pages handle their own gating, the
# health probe must answer monitors, and the login page pulls in static
# assets + the service worker before the user is signed in.
_OPEN_PATHS = {"/healthz", "/sw.js", "/favicon.ico"}
_OPEN_PREFIXES = ("/static/",)

_PBKDF2_ROUNDS = 600_000
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days, matching the CSRF cookie


def auth_disabled() -> bool:
    """True only when WEB_AUTH is the literal 'none'. Blank/unset leaves auth
    ON — disabling is a deliberate opt-out, never the side effect of an empty
    field. Read live from the env so it tracks the running environment."""
    return os.environ.get("WEB_AUTH", "").strip().lower() == "none"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def _verify_hash(stored: str, password: str) -> bool:
    try:
        algo, rounds, salt_hex, want_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                  bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, AttributeError):
        return False
    return secrets.compare_digest(got.hex(), want_hex)


def _read() -> dict:
    try:
        data = json.loads(cfg.WEB_AUTH_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def credentials_configured() -> bool:
    d = _read()
    return bool(d.get("username") and d.get("password_hash")
                and d.get("session_secret"))


def set_credentials(username: str, password: str) -> bool:
    """Persist username + password hash + a fresh session secret, atomically
    and 0600. Returns False if the data volume isn't writable so callers can
    show a clear message instead of 500ing. The new session secret rotates on
    every call, so resetting the password logs out any existing browser."""
    payload = {
        "username": username,
        "password_hash": hash_password(password),
        "session_secret": secrets.token_urlsafe(32),
    }
    try:
        cfg.WEB_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(cfg.WEB_AUTH_FILE.parent),
                                   prefix=".qobuz_web_auth.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, cfg.WEB_AUTH_FILE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        return False
    return True


def verify_login(username: str, password: str) -> bool:
    """Constant-time check of both fields. The password is always run through
    the KDF when a hash exists, so a wrong username and a wrong password take
    the same time and neither is distinguishable by timing."""
    d = _read()
    stored_hash = d.get("password_hash") or ""
    if not stored_hash:
        return False
    user_ok = secrets.compare_digest(username, d.get("username") or "")
    pass_ok = _verify_hash(stored_hash, password)
    return user_ok and pass_ok


def session_value() -> str:
    """The value a signed-in browser carries — the persisted session secret."""
    return _read().get("session_secret") or ""


def verify_session(cookie_value: str) -> bool:
    secret = session_value()
    if not secret or not cookie_value:
        return False
    return secrets.compare_digest(cookie_value, secret)


def _secure(request) -> bool:
    return (request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https")


def set_session_cookie(response, request) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_value(),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_secure(request),
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, samesite="lax")


def auth_active() -> bool:
    """Auth is both enabled and set up — the only state in which a Log out
    control makes sense. Exposed to templates as a global."""
    return not auth_disabled() and credentials_configured()


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate every route behind a session cookie once a login is configured.

    Sits inside the CSRF middleware so the login/setup POSTs still get CSRF
    validation and the redirects it returns still pick up the CSRF cookie and
    security headers on the way out.
    """

    async def dispatch(self, request, call_next):
        if auth_disabled():
            return await call_next(request)
        path = request.url.path
        if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            return await call_next(request)

        creds = _read()
        configured = bool(creds.get("username") and creds.get("password_hash")
                          and creds.get("session_secret"))
        if not configured:
            # Nothing protects the box yet — force the setup screen, but let
            # the setup GET/POST through so a login can actually be created.
            if path == SETUP_PATH:
                return await call_next(request)
            return self._reject(request, SETUP_PATH)

        cookie = request.cookies.get(SESSION_COOKIE)
        secret = creds.get("session_secret") or ""
        if cookie and secret and secrets.compare_digest(cookie, secret):
            return await call_next(request)
        if path == LOGIN_PATH:
            return await call_next(request)
        return self._reject(request, LOGIN_PATH)

    @staticmethod
    def _reject(request, location):
        # API/SSE callers get a machine-readable 401. htmx requests get a
        # full-page redirect header (a 303 body would be swapped into a
        # fragment). Everything else is an ordinary browser redirect.
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "authentication required"},
                                status_code=401)
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=401, headers={"HX-Redirect": location})
        return RedirectResponse(url=location, status_code=303)
