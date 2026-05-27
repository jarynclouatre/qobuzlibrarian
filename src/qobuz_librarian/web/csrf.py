"""CSRF protection — double-submit cookie with SameSite=Strict."""
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

CSRF_COOKIE_NAME = "qf_csrf"
CSRF_FORM_FIELD = "_csrf_token"
CSRF_HEADER = "X-CSRF-Token"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_FORM_BYTES = 1 * 1024 * 1024  # 1 MB — no form on this app gets close


def _new_token() -> str:
    return secrets.token_urlsafe(32)


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
        token = cookie_token or _new_token()
        request.state.csrf_token = token

        if request.method not in _SAFE_METHODS:
            try:
                content_length = int(request.headers.get("content-length") or 0)
            except ValueError:
                content_length = 0
            if content_length > _MAX_FORM_BYTES:
                return PlainTextResponse("Request body too large",
                                         status_code=413)
            submitted = request.headers.get(CSRF_HEADER)
            if not submitted and 0 < content_length <= _MAX_FORM_BYTES:
                # Read the body only when its length is declared and bounded —
                # a chunked request with no Content-Length is never read here, so
                # a tokenless POST can't make us buffer an unbounded body. body()
                # (not form()) lets BaseHTTPMiddleware replay it to downstream
                # Form() handlers, which would otherwise 422.
                try:
                    body = await request.body()
                    ct = request.headers.get("content-type", "")
                    if "application/x-www-form-urlencoded" in ct:
                        from urllib.parse import parse_qs
                        parsed = parse_qs(body.decode("latin-1"))
                        submitted = (parsed.get(CSRF_FORM_FIELD) or [None])[0]
                    # multipart/form-data isn't parsed here (doing so would
                    # consume the stream before downstream Form() handlers): a
                    # multipart POST must carry the token in the CSRF header. The
                    # htmx config adds it to every AJAX request; there are no
                    # hand-built multipart forms today, so this is just a guard
                    # for anyone adding a file-upload form later.
                except Exception:
                    submitted = None
            if not cookie_token or not submitted or not secrets.compare_digest(
                str(cookie_token).encode("utf-8"), str(submitted).encode("utf-8")
            ):
                return PlainTextResponse(
                    "CSRF token missing or invalid", status_code=403
                )

        response = await call_next(request)
        if not cookie_token:
            secure = (request.url.scheme == "https"
                      or request.headers.get("x-forwarded-proto") == "https")
            response.set_cookie(
                CSRF_COOKIE_NAME,
                token,
                max_age=60 * 60 * 24 * 30,
                samesite="strict",
                # The page reads the token from its <meta> tag, never from
                # this cookie, so HttpOnly costs nothing and keeps it out of
                # reach of any injected script.
                httponly=True,
                secure=secure,
            )
        return response


# script-src carries a per-request nonce (set on request.state.csp_nonce below)
# rather than 'unsafe-inline', so a reflected/injected <script> can't run — only
# the few inline blocks we mint with this request's nonce do. The SSE/progress
# logic that htmx swaps in lives in the external app.js ('self'); a nonce can't
# survive an htmx swap because the fragment's nonce wouldn't match the live
# document's. style-src keeps 'unsafe-inline': the Tailwind/daisyUI build and
# htmx's indicator styles lean on inline style, and autoescape is the real XSS
# guard — locking styles down is a separate, far larger surface.
def _csp(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://static.qobuz.com; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds X-Content-Type-Options, X-Frame-Options, Referrer-Policy,
    Permissions-Policy, a nonce-based Content-Security-Policy, and HSTS on
    HTTPS requests only."""
    async def dispatch(self, request, call_next):
        # Minted before the route renders so templates can stamp it on their
        # inline <script>s via request.state.csp_nonce (mirrors csrf_token).
        nonce = _new_token()
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", _csp(nonce))
        # The UI uses no device sensors; deny them so an injected script can't
        # reach for the camera/mic/location either.
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        # HSTS only on HTTPS — emitting it over plain HTTP is pointless and
        # would brick a user who later reaches the host via HTTP.
        is_https = (request.url.scheme == "https"
                    or request.headers.get("x-forwarded-proto") == "https")
        if is_https:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000")
        return response


class StripServerHeaderMiddleware:
    """ASGI middleware that drops the Server header before it leaves the
    process — uvicorn advertises itself by default, which is a free hint
    to anyone scanning for known framework CVEs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def _send(msg):
            if msg["type"] == "http.response.start":
                msg["headers"] = [
                    (n, v) for (n, v) in msg.get("headers", [])
                    if n.lower() != b"server"
                ]
            await send(msg)
        await self.app(scope, receive, _send)
