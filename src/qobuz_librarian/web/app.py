"""FastAPI web application for Qobuz Librarian."""
import asyncio
import concurrent.futures
import html
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import NoCredsError
from qobuz_librarian.web import auth as web_auth
from qobuz_librarian.web import jobs as job_mgr
from qobuz_librarian.web.csrf import (
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    StripServerHeaderMiddleware,
)

# Held for the lifetime of the web process. Module-level so Python won't
# garbage-collect it (which would silently release the flock).
_RUN_LOCK_HANDLE = None
# Set when run_lock.acquire() fails at startup — the holder's PID, used by
# every destructive route to refuse new work. Read-only routes (dashboard,
# search, settings view) stay open so the user can still figure out what's
# going on.
_LOCK_BUSY_PID = None
# True when the web app has deliberately released the run-lock so the terminal
# (CLI) can use it — set by the Settings "Mode" toggle, or at startup when
# QL_CLI_ONLY is set. Distinct from _LOCK_BUSY_PID (another process grabbed the
# lock unexpectedly): in CLI mode the web holds no lock on purpose and pauses
# its own download/scan endpoints so the two can't race over /staging.
_CLI_MODE = False
# Tri-state result of the startup token probe. None until the probe runs
# (or if the network glitched); True if Qobuz accepted the saved token;
# False if Qobuz returned AuthLost. The dashboard banner only fires on the
# explicit False so a transient network blip doesn't nag the user.
_TOKEN_VALID: bool | None = None


def _lock_busy_response(request):
    """Return a 503 response if the run-lock is busy OR a critical volume
    was unwritable at startup, else None."""
    if _CLI_MODE:
        msg = ("Terminal (CLI) mode is on — the web app has handed the run-lock "
               "to the terminal, so downloads and scans are paused here. Run "
               "your CLI commands, then switch back on Settings → Mode "
               "(Resume web app).")
    elif _LOCK_BUSY_PID is not None:
        msg = (f"Another Qobuz Librarian instance holds the run-lock "
               f"(pid {_LOCK_BUSY_PID}). Stop that invocation first "
               f"(likely a `docker compose run` CLI session left open), "
               f"then restart this container to reacquire the lock.")
    elif _UNWRITABLE_VOLUMES:
        msg = (f"Required volume(s) not writable: "
               f"{', '.join(_UNWRITABLE_VOLUMES)}. On a NAS, set "
               "PUID/PGID to the share owner and confirm the host "
               "directories exist. Downloads can't run until fixed.")
    else:
        return None
    if _is_htmx(request):
        return HTMLResponse(
            f'<div class="alert alert-error" data-flash>{html.escape(msg)}</div>',
            status_code=200)
    return _tr(request, "lock_busy.html", {"msg": msg}, status_code=503)


# Populated at startup. Empty list means OK; non-empty means destructive
# POSTs return 503 until the container restarts with the volumes mounted.
_UNWRITABLE_VOLUMES: list[str] = []


def _resume_album_download(job, _args):
    from qobuz_librarian.web import flows
    return lambda j, chosen: flows.execute_albums(j, chosen, _get_token())


def _resume_upgrade(job, _args):
    from qobuz_librarian.web import flows
    return lambda j, chosen: flows.execute_upgrades(j, chosen, _get_token())


def _resume_repair(job, _args):
    from qobuz_librarian.web import flows
    return lambda j, chosen: flows.execute_repairs(j, chosen, _get_token())


def _resume_migration(job, args):
    from qobuz_librarian.web import flows
    dest = args.get("dest", "")
    in_place = bool(args.get("in_place"))
    src = args.get("src")
    return lambda j, chosen: flows.execute_migration(
        j, chosen, dest, in_place=in_place,
        src=Path(src) if src else None)


def _resume_downsample(job, _args):
    from qobuz_librarian.web import flows
    return lambda j, chosen: flows.execute_downsamples(j, chosen)


# Names the persisted ``execute_kind`` strings so jobs survive a restart
# even though their original execute closure is gone. Each factory is
# called lazily, when the user actually approves the reloaded job, so
# the rebound function reads the current token rather than baking in the
# (possibly-rotated) one from the prior session.
_RESUME_EXECUTE: dict = {
    "album":        _resume_album_download,
    "library":      _resume_album_download,
    "new_releases": _resume_album_download,
    "upgrade":      _resume_upgrade,
    "repair":       _resume_repair,
    "migration":    _resume_migration,
    "downsample":   _resume_downsample,
}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    import logging
    import os
    import shutil
    global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _CLI_MODE
    _log = logging.getLogger("qobuz_librarian")
    from qobuz_librarian.ui_cli.logging import attach_file_handler
    attach_file_handler(cfg.APP_LOG_FILE, cfg.LOG_LEVEL)
    if web_auth.auth_disabled():
        _log.warning("[warn] WEB_AUTH=none — web UI is unauthenticated, do not "
                     "expose to an untrusted network")
    else:
        cred_status = web_auth.apply_env_credentials()
        if cred_status in ("applied", "applied_weak"):
            _log.info("Configured the web login from WEB_AUTH_USER / "
                      "WEB_AUTH_PASSWORD.")
            if cred_status == "applied_weak":
                _log.warning(
                    "WEB_AUTH_PASSWORD is shorter than %d characters — it's the "
                    "only thing gating the web UI; use a longer one.",
                    web_auth.MIN_PASSWORD_LEN)
        elif cred_status == "partial":
            _log.warning("Set both WEB_AUTH_USER and WEB_AUTH_PASSWORD to seed "
                         "the web login from the environment — only one was set.")
        elif cred_status == "failed":
            _log.warning("Couldn't write the web login from the environment; "
                         "the data volume may not be writable.")
        if not web_auth.credentials_configured():
            _log.warning(
                "No web login configured — the open /setup screen is reachable "
                "to whoever hits the port first, who would then own the admin "
                "account. Seed WEB_AUTH_USER / WEB_AUTH_PASSWORD (compose) to "
                "close this window, and complete setup promptly on a trusted "
                "network.")
    from qobuz_librarian import run_lock
    from qobuz_librarian.api.auth import sync_streamrip_creds_from_env
    from qobuz_librarian.web import settings_store
    settings_store.load()
    # If creds are provided via env vars, mirror them into the streamrip
    # config now so web-triggered downloads don't fail on streamrip's
    # interactive auth prompt (the env-var path doesn't otherwise reach
    # streamrip's own config file).
    if sync_streamrip_creds_from_env() is False:
        _log.warning("Couldn't write env credentials into the streamrip "
                     "config; web downloads may fail until creds are set "
                     "via the Settings page.")
    # Acquire the same run lock the CLI uses, so a `docker compose run ...
    # cli` invocation while the web is up can't corrupt /staging. If we
    # CAN'T acquire it, _LOCK_BUSY_PID is set and every destructive route
    # refuses with 503 — silently continuing without the lock would let
    # two writers race in /staging and corrupt both their downloads.
    if os.environ.get("QL_CLI_ONLY", "").strip().lower() in ("1", "true", "yes", "on"):
        # Terminal-first deployment: don't take the lock, so `docker exec ...
        # qobuz-librarian` always works. The web UI still serves for browsing
        # and Settings; its download/scan endpoints stay paused until the user
        # resumes web mode (which lasts until the next restart).
        _CLI_MODE = True
        _LOCK_BUSY_PID = None
        _log.info("QL_CLI_ONLY set — starting in terminal (CLI) mode; the web "
                  "app holds no lock and download/scan endpoints are paused.")
    else:
        _CLI_MODE = False
        try:
            _RUN_LOCK_HANDLE = run_lock.acquire()
            _LOCK_BUSY_PID = None
        except run_lock.LockBusy as busy:
            _LOCK_BUSY_PID = busy.pid
            _log.error(
                "STARTUP: another Qobuz Librarian run holds the lock (pid %s). "
                "Background task will retry acquisition every 30s; in the "
                "meantime download/scan endpoints will return 503.",
                busy.pid,
            )

    async def _retry_lock():
        global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID
        while _LOCK_BUSY_PID is not None:
            await asyncio.sleep(30)
            # The lock state can change during the sleep: set_mode('cli') hands
            # the lock to the terminal (sets _CLI_MODE, clears _LOCK_BUSY_PID).
            # Re-check before acquiring, or we'd grab the lock back in CLI mode
            # and wedge both the web app and the CLI until restart.
            if _LOCK_BUSY_PID is None or _CLI_MODE:
                return
            try:
                _RUN_LOCK_HANDLE = run_lock.acquire()
                _LOCK_BUSY_PID = None
                _log.info("Lock acquired; web write endpoints now active.")
                return
            except run_lock.LockBusy as busy:
                _LOCK_BUSY_PID = busy.pid
    if _LOCK_BUSY_PID is not None:
        asyncio.create_task(_retry_lock())

    _UNWRITABLE_VOLUMES.clear()
    # Opt-in via env so tests / dev runs that don't have /staging /music
    # mounted don't trip on the gate. The bundled compose sets this to 1.
    if os.environ.get("QL_CHECK_VOLUMES") == "1":
        # The label (the container-internal mount name) is what the operator
        # checks in their compose.yaml; the resolved cfg path is what's
        # actually being tested. Showing both makes "/music" warnings useful
        # even when MUSIC_ROOT is customised away from the bundled default,
        # and is_dir() catches a /dev/null-shaped mistake the W_OK alone misses.
        for label, path in (("STAGING_DIR", cfg.STAGING_DIR),
                            ("MUSIC_ROOT", cfg.MUSIC_ROOT)):
            p = Path(path)
            unreachable = not p.exists()
            not_a_dir = p.exists() and not p.is_dir()
            unwritable = p.exists() and p.is_dir() and not os.access(str(p), os.W_OK)
            if unreachable or not_a_dir or unwritable:
                _UNWRITABLE_VOLUMES.append(
                    f"{label}={path!s}"
                    + (" (missing)" if unreachable
                       else " (not a directory)" if not_a_dir
                       else " (read-only)"))
        if _UNWRITABLE_VOLUMES:
            _log.error("STARTUP: critical volumes not usable: %s. Write "
                       "endpoints will return 503 until container restarts "
                       "with mounts fixed.", _UNWRITABLE_VOLUMES)
    # Housekeeping the CLI also runs on each invocation — must be done
    # here too, otherwise a web-only deployment never sweeps stale upgrade
    # backups or orphan lyric-state entries.
    try:
        from qobuz_librarian.library.backup import cleanup_old_upgrade_backups
        n = cleanup_old_upgrade_backups()
        if n:
            _log.info("Cleaned up %d stale upgrade backup(s) at startup.", n)
    except Exception as e:
        _log.debug("upgrade-backup cleanup error at startup: %s", e)
    try:
        from qobuz_librarian.integrations.lyrics import _prune_lyric_state_orphans
        _prune_lyric_state_orphans()
    except Exception as e:
        _log.debug("lyric-state prune error at startup: %s", e)
    # Heavy, throttled maintenance: prune_missing() stats every cached file
    # (100k+ on a NAS library), so run it in the background instead of blocking
    # the app from serving its first request.
    async def _bg_prune_flac_cache():
        try:
            from qobuz_librarian.library import flac_cache
            n_pruned = await asyncio.get_running_loop().run_in_executor(
                None, flac_cache.prune_missing)
            if n_pruned:
                _log.info("Pruned %d stale tag-cache entries.", n_pruned)
        except Exception as e:
            _log.debug("flac-cache prune error: %s", e)
    asyncio.create_task(_bg_prune_flac_cache())
    job_mgr.start_worker()
    if not shutil.which("rip"):
        _log.warning("`rip` (streamrip) not found in PATH — downloads will fail")
    if not shutil.which("beet"):
        _log.warning("`beet` (beets) not found in PATH — imports will fail")
    if not shutil.which("flac"):
        _log.warning("`flac` not found — FLAC integrity checks fall back to a size heuristic")
    if not shutil.which("ffmpeg"):
        _log.warning("`ffmpeg` not found — hi-res downsampling disabled")
    # Reload jobs from the prior session so an AWAITING_REVIEW scan's
    # candidates survive a container restart and queued/running downloads
    # don't silently vanish (they're rebadged FAILED with a retry hint).
    # Runs AFTER start_worker() above — benign because the work queues are empty
    # until restore re-queues into them, and the registry is lock-guarded, so the
    # worker only idle-ticks until restore hands it something.
    try:
        job_mgr.restore_jobs(_RESUME_EXECUTE)
    except Exception as e:
        _log.warning("couldn't restore prior jobs: %s — starting fresh.", e)
    # Probe the saved token against Qobuz so a stale slot — non-empty but
    # not actually authenticated — surfaces in the dashboard banner rather
    # than failing the user's first search.
    asyncio.create_task(_probe_token())
    # Keep the dashboard banner honest after startup: any in-session 401 from
    # the API client flips _TOKEN_VALID to False here, so a token that expires
    # mid-session shows "saved token isn't authenticating" immediately instead
    # of leaving stale green until the user happens to retry the failed action.
    from qobuz_librarian.api.auth import register_auth_state_listener
    register_auth_state_listener(_on_auth_state)
    yield
    # Release the flock explicitly — assigning None alone relies on GC
    # closing the file, which may not happen if any caller still holds
    # a reference.
    if _RUN_LOCK_HANDLE is not None:
        try:
            _RUN_LOCK_HANDLE.close()
        except OSError:
            pass
        _RUN_LOCK_HANDLE = None


def _classify_token(token):
    """Ask Qobuz whether a token works.

    Returns "ok", "rejected" (Qobuz refused the token), or "unreachable"
    (couldn't tell — network down, timeout, or a Qobuz-side hiccup). A 401
    or a 400 means Qobuz parsed the request and turned the token away;
    everything else is treated as inconclusive so a transient blip doesn't
    look like a bad token.
    """
    from qobuz_librarian.api.auth import AuthLost, QobuzError, friendly_qobuz_error
    from qobuz_librarian.api.client import qobuz_get
    try:
        qobuz_get("album/search", {"query": "ok", "limit": 1}, token)
        return "ok"
    except AuthLost:
        return "rejected"
    except QobuzError as e:
        return "rejected" if friendly_qobuz_error(e).startswith("HTTP 400") \
            else "unreachable"
    except Exception:
        return "unreachable"


def _on_auth_state(valid: bool) -> None:
    """Listener registered with api.auth so a 401 mid-session flips the
    dashboard banner without waiting for the next page-load probe."""
    global _TOKEN_VALID
    _TOKEN_VALID = bool(valid)


async def _probe_token():
    """One-shot startup check that the saved token still authenticates.

    Sets ``_TOKEN_VALID`` to True/False/None: None means the result is
    inconclusive (no token saved, or the probe couldn't reach Qobuz), so
    the dashboard treats it as "don't nag yet."
    """
    global _TOKEN_VALID
    creds = _read_creds()
    if not creds.get("auth_token"):
        return
    token = creds["auth_token"]
    from qobuz_librarian.api.client import call_within
    try:
        verdict = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, lambda: call_within(cfg.WEB_TEST_AUTH_TIMEOUT,
                                          _classify_token, token)),
            timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        verdict = "unreachable"
    if verdict == "ok":
        _TOKEN_VALID = True
    elif verdict == "rejected":
        _TOKEN_VALID = False


app = FastAPI(title="Qobuz Librarian", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)

# AuthMiddleware is added first so it ends up innermost — it runs after the
# CSRF middleware, which keeps CSRF validation on the login/setup POSTs and
# lets the redirects it returns pick up the CSRF cookie + security headers.
app.add_middleware(web_auth.AuthMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(StripServerHeaderMiddleware)

_here = Path(__file__).parent
templates = Jinja2Templates(directory=str(_here / "templates"))

try:
    from importlib.metadata import version as _pkg_version
    _APP_VERSION = _pkg_version("qobuz-librarian")
except Exception:
    # Only reached on a broken / non-installed editable run; "unknown" is
    # honest, a hardcoded number here just goes stale on the next bump.
    _APP_VERSION = "unknown"
templates.env.globals["app_version"] = _APP_VERSION
templates.env.globals["repo_url"] = "https://github.com/jarynclouatre/qobuz-librarian"
# Whether to show a Log out control — true only when auth is on and set up.
templates.env.globals["auth_active"] = web_auth.auth_active

static_dir = _here / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# Bake the running version into the worker so its cache name changes on every
# release. The script bytes then differ release-to-release, which is what makes
# the browser actually pick up the new worker and purge the stale caches —
# a fixed cache name left returning visitors on old assets after an upgrade.
_SW_JS = (static_dir / "sw.js").read_text(encoding="utf-8").replace(
    "__APP_VERSION__", _APP_VERSION)


@app.get("/sw.js")
async def service_worker():
    return Response(
        _SW_JS,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/healthz")
async def healthz():
    """Cheap liveness probe for HEALTHCHECK / uptime monitors."""
    return JSONResponse({"ok": True})


@app.head("/healthz")
async def healthz_head():
    """Uptime monitors HEAD before GET — return a body-less 200 so they
    don't mark the service down on a 405."""
    return Response(status_code=200)


@app.head("/queue")
async def queue_head():
    return Response(status_code=200)


@app.head("/settings")
async def settings_head():
    return Response(status_code=200)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if not web_auth.credentials_configured():
        return RedirectResponse(url="/setup", status_code=303)
    cookie = request.cookies.get(web_auth.SESSION_COOKIE)
    if cookie and web_auth.verify_session(cookie):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html",
                                      context={"error": ""})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(""),
                       password: str = Form("")):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if not web_auth.credentials_configured():
        return RedirectResponse(url="/setup", status_code=303)
    ip = (request.client.host if request.client else "") or "unknown"
    if not web_auth.check_login_rate_limit(ip, username):
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Too many failed attempts — wait an hour and try again."},
            status_code=429)
    if not web_auth.verify_login(username.strip(), password):
        web_auth.record_login_failure(ip, username)
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Incorrect username or password."},
            status_code=401)
    web_auth.clear_login_failures(ip, username)
    resp = RedirectResponse(url="/", status_code=303)
    web_auth.set_session_cookie(resp, request)
    return resp


@app.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse(url="/login", status_code=303)
    # Revoke the session server-side, not just the browser cookie — otherwise a
    # captured cookie value stays valid for its full 30-day lifetime.
    web_auth.revoke_session(request.cookies.get(web_auth.SESSION_COOKIE))
    web_auth.clear_session_cookie(resp)
    return resp


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if web_auth.credentials_configured():
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="setup.html",
                                      context={"error": "", "username": ""})


@app.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request, username: str = Form(""),
                       password: str = Form(""), confirm: str = Form("")):
    if web_auth.auth_disabled():
        return RedirectResponse(url="/", status_code=303)
    if web_auth.credentials_configured():
        return RedirectResponse(url="/", status_code=303)
    user = username.strip()
    if not user:
        err = "Pick a username."
    elif len(password) < web_auth.MIN_PASSWORD_LEN:
        err = f"Use a password of at least {web_auth.MIN_PASSWORD_LEN} characters."
    elif password != confirm:
        err = "The two passwords don't match."
    else:
        err = ""
    if err:
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={"error": err, "username": user}, status_code=400)
    # First-run setup is unauthenticated by necessity (no creds exist yet), so
    # whoever reaches the open port first claims admin. Log the client IP so the
    # takeover window is at least auditable; the prevention is seeding
    # WEB_AUTH_USER/WEB_AUTH_PASSWORD (now plumbed through compose).
    _ip = (request.client.host if request.client else "") or "unknown"
    import logging as _logging
    _logging.getLogger("qobuz_librarian").warning(
        "First-run /setup creating admin account from %s (username=%r).",
        _ip, user)
    if not web_auth.set_credentials(user, password):
        return templates.TemplateResponse(
            request=request, name="setup.html",
            context={"error": "Couldn't save the login — the data volume "
                              "isn't writable. Check PUID/PGID and volume "
                              "permissions.", "username": user},
            status_code=500)
    resp = RedirectResponse(url="/", status_code=303)
    web_auth.set_session_cookie(resp, request)
    return resp


def _tr(request, name, context, *, status_code=200):
    """TemplateResponse wrapper for Starlette 1.0+ signature.

    The navbar badge is computed once per full-page render and injected via
    context; partial-fragment renders skip this entirely. A route that already
    fetched the active job list for its own template (`/queue`, the dashboard)
    can pass it as `pending` and the badge derives from that — no second
    `pending_and_running()` call on the same render.
    """
    if "pending_job_count" not in context or "queue_has_running" not in context:
        active = context.get("pending") or job_mgr.registry.pending_and_running()
        context.setdefault("pending_job_count", len(active))
        context.setdefault(
            "queue_has_running",
            any(j.status.value in ('running', 'scanning') for j in active),
        )
    context.setdefault("cli_mode", _CLI_MODE)
    # Error/utility renders (e.g. the 404 page) don't name a nav section; an
    # explicit empty page just leaves every nav link inactive instead of
    # relying on Jinja's undefined-is-falsey behaviour.
    context.setdefault("page", "")
    # Standing health the navbar surfaces on every page, not just the dashboard:
    # a rejected token (auth lost mid-session) and a lock held by another
    # instance both stop downloads, and a user on Search/Queue shouldn't only
    # find out when a job fails. Both are cheap module-level flags — no I/O.
    context.setdefault("health_token_invalid", _TOKEN_VALID is False)
    context.setdefault("health_lock_busy", bool(_LOCK_BUSY_PID))
    return templates.TemplateResponse(request=request, name=name,
                                      context=context, status_code=status_code)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Render a styled page for a mistyped/stale URL instead of a bare
    ``{"detail": "Not Found"}``. API routes and every non-404 status keep the
    JSON shape callers expect."""
    if exc.status_code == 404 and not request.scope["path"].startswith("/api/"):
        return _tr(request, "error.html", {
            "code": 404,
            "title": "Page not found",
            "msg": "That page doesn't exist — the link may have moved or been "
                   "mistyped.",
        }, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


# Serialises the dedupe-check-then-submit in queue_download: the network
# get_album() await between the early check and the submit leaves a window where
# two requests for one album both pass the check and queue it twice.
_DOWNLOAD_SUBMIT_LOCK = threading.Lock()


def _find_job_touching_album(album_id: str):
    """Return a pending/running/awaiting-review job that already covers
    album_id, either as its direct subject or as one of its candidates."""
    for j in job_mgr.registry.pending_and_running():
        if j.album_id == album_id:
            return j
        # Snapshot: a SCANNING job appends to candidates from the worker thread,
        # and iterating it live can raise "list changed size during iteration".
        for cand in list(j.candidates or []):
            payload = cand.get("payload") or {}
            if payload.get("album_id") == album_id:
                return j
            qa = (payload.get("candidate") or {}).get("qobuz_album") or {}
            if qa.get("id") == album_id:
                return j
    return None


def _staging_album_count() -> int:
    """Album folders left in staging by an interrupted import. The CLI warns
    about these at startup (`_check_staging_occupied`); the web has no such
    signal, so a crash mid-import leaves web-only users with no idea files are
    stranded. Only meaningful when nothing is actively writing — the caller
    suppresses the banner while a job is running."""
    try:
        return sum(1 for d in cfg.STAGING_DIR.iterdir() if d.is_dir())
    except OSError:
        return 0


@app.head("/")
async def dashboard_head():
    """Uptime monitors / curl -I hit HEAD before GET; serve a body-less 200
    so they don't get a 405 and mark the service down."""
    return Response(status_code=200)


# Reentrant so the auto-triggers (which hold it) can call the _start_* helpers
# below (which re-acquire it). It makes every "is one already queued? → submit"
# check-and-submit atomic across both the manual POST and the dashboard auto
# path, so a manual click landing alongside an auto-trigger can't stack two.
_auto_check_lock = threading.RLock()


def _existing_new_release_check():
    """An active or awaiting-review new-release check, or None — so a second one
    isn't stacked on top of one already queued or waiting for review."""
    for j in job_mgr.registry.pending_and_running():  # ACTIVE incl awaiting_review
        if getattr(j, "execute_kind", "") == "new_releases":
            return j
    return None


def _start_new_release_check():
    """Submit a whole-library new-release check and return the job (or the one
    already queued). Shared by the manual Library-page option and the automatic
    dashboard trigger."""
    with _auto_check_lock:
        # The run-lock may have been handed to the terminal mid-submit (this can
        # run in an executor for POST /library). Re-check under the lock so a
        # scan can't start right after set_mode('cli') released the lock and then
        # race the CLI over /staging.
        if _CLI_MODE:
            return None
        existing = _existing_new_release_check()
        if existing is not None:
            return existing
        from qobuz_librarian.web import flows
        job = job_mgr.Job(title="New-release check")
        job.execute_kind = "new_releases"
        job_mgr.submit_scan(
            job,
            lambda j: flows.scan_new_releases(j, _get_token()),
            lambda j, chosen: flows.execute_albums(j, chosen, _get_token()),
        )
        return job


def _new_release_review():
    """The awaiting-review new-release check for the dashboard badge, if any."""
    for j in job_mgr.registry.awaiting_review():
        if getattr(j, "execute_kind", "") == "new_releases":
            return {"id": j.id, "count": len(j.candidates)}
    return None


def _maybe_auto_check_new_releases():
    """Quietly run the new-release check on dashboard load when it's due.

    Read-only — it only parks a review list, never downloads — so it's safe to
    fire from a GET. Skipped when the check is off, the token is missing or
    known-bad, the CLI holds the lock, another job is actively working, a
    new-release list is already awaiting review, or the interval hasn't elapsed.
    """
    if cfg.NEW_RELEASE_CHECK_INTERVAL <= 0 or _CLI_MODE or _LOCK_BUSY_PID is not None:
        return
    # Don't bother (or thrash) when there's no token, or one we already know
    # Qobuz is rejecting — it would just fail on the first call every load.
    if _TOKEN_VALID is False or not _read_creds().get("auth_token"):
        return
    from qobuz_librarian.library import new_releases
    # Only after a full library scan has established the baseline — otherwise the
    # check would crawl every artist just to record a starting point and surface
    # nothing. A completed library scan seeds it (flows.scan_library).
    if not new_releases.is_baseline_complete():
        return
    # Serialise the check-and-submit so two concurrent dashboard loads can't
    # both pass the gate and queue the check twice.
    with _auto_check_lock:
        active = job_mgr.registry.pending_and_running()
        working = any(j.status != job_mgr.JobStatus.AWAITING_REVIEW for j in active)
        pending_check = any(getattr(j, "execute_kind", "") == "new_releases"
                            for j in active)
        if working or pending_check:
            return
        last = new_releases.last_run()
        if last is not None and (time.time() - last) < cfg.NEW_RELEASE_CHECK_INTERVAL:
            return
        # Stamp the attempt before submitting: the scan only advances the stamp
        # on a clean finish, so without this a failed/cancelled run would re-fire
        # on every load.
        new_releases.touch_run()
        _start_new_release_check()


def _active_scan(*kinds, statuses=("pending", "scanning")):
    """A job of one of the given execute_kinds in one of ``statuses``, or None —
    used to fold a double-submitted pass onto the one already in flight instead
    of stacking duplicate work. Defaults to the scan phase: a scan keeps its
    execute_kind through the post-review download (which runs as ``running``),
    so matching only pending/scanning lets a deliberate re-scan still queue
    behind a batch that's downloading. Run-to-completion jobs with no review
    (lyrics) pass their own running phase instead."""
    for j in job_mgr.registry.pending_and_running():
        if getattr(j, "execute_kind", "") in kinds and j.status.value in statuses:
            return j
    return None


async def _submit_scan_deduped_async(job, scan_fn, execute_fn, *kinds, **kw):
    """Run _submit_scan_deduped off the event loop.

    It takes _auto_check_lock, which dashboard executor threads can hold across
    small (possibly NAS-backed) reads, so the loop must not block on it — the
    same reason POST /library offloads its submit. Every async scan route goes
    through this instead of calling _submit_scan_deduped directly on the loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _submit_scan_deduped(job, scan_fn, execute_fn, *kinds, **kw))


def _submit_scan_deduped(job, scan_fn, execute_fn, *kinds, statuses=("pending", "scanning")):
    """Submit a scan only if one of ``kinds`` isn't already active, atomically.

    Checking _active_scan and submitting in one locked step closes the window
    where two near-simultaneous POSTs (a double-click, or the auto-trigger
    landing with a manual click) both pass the check and stack duplicate scans.
    Returns the job to redirect to — the new one, or the in-flight duplicate."""
    with _auto_check_lock:
        existing = _active_scan(*kinds, statuses=statuses)
        if existing is not None:
            return existing
        job_mgr.submit_scan(job, scan_fn, execute_fn)
        return job


def _active_library_scan():
    """A library scan that's already pending/crawling, or None."""
    return _active_scan("library")


def _start_library_scan(partial_only=False):
    """Submit a library scan and return the job. Shared by the Library page and
    the automatic first-run/resume trigger. scan_library resumes from a matching
    checkpoint on its own, so this is the same call whether starting or resuming.

    Deduped under the lock: if a library scan is already crawling, return it
    instead of stacking a second one (the manual button and the auto trigger can
    both land here at once)."""
    with _auto_check_lock:
        # Re-check the CLI handoff under the lock (see _start_new_release_check).
        if _CLI_MODE:
            return None
        existing = _active_library_scan()
        if existing is not None:
            return existing
        from qobuz_librarian.web import flows
        title = "Library album-fill scan" if partial_only else "Library gap scan"
        job = job_mgr.Job(title=title)
        job.execute_kind = "library"
        job_mgr.submit_scan(
            job,
            lambda j: flows.scan_library(j, _get_token(), partial_only=partial_only),
            lambda j, chosen: flows.execute_albums(j, chosen, _get_token()),
        )
        return job


def _maybe_auto_first_scan():
    """On first run, auto-start a library scan so the new-release baseline gets
    established (and missing albums surface); also resume an interrupted one.

    A fresh first scan is started once (so cancelling it doesn't relaunch it on
    every load); an interrupted scan leaves a checkpoint and is resumed whenever
    the app is idle, driving it to completion across restarts. Off via
    AUTO_LIBRARY_SCAN — a manual scan still resumes the checkpoint and seeds the
    baseline.
    """
    if not cfg.AUTO_LIBRARY_SCAN or _CLI_MODE or _LOCK_BUSY_PID is not None:
        return
    if _TOKEN_VALID is False or not _read_creds().get("auth_token"):
        return
    from qobuz_librarian.library import new_releases, scan_checkpoint
    if new_releases.is_baseline_complete():
        return
    with _auto_check_lock:
        if any(j.status != job_mgr.JobStatus.AWAITING_REVIEW
               for j in job_mgr.registry.pending_and_running()):
            return  # something already working
        cp = scan_checkpoint.pending()
        if cp is not None:
            _start_library_scan(partial_only=(cp["kind"] == "partial"))
        elif not new_releases.auto_scan_attempted():
            new_releases.note_auto_scan_attempted()
            _start_library_scan()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from qobuz_librarian import config as _cfg
    active_jobs = [j for j in job_mgr.registry.pending_and_running()
                   if j.status.value in ('running', 'scanning')]

    # These all read the (often NAS / network-mounted) data + music volumes —
    # the fetch log, the creds file, the lyric-retry file, and a staging
    # iterdir(). Run them off the event loop so a sleepy/flaky mount can't stall
    # every other request (health checks, SSE setup, search) while it blocks.
    def _gather_disk_state():
        from qobuz_librarian.integrations.lyrics import load_lyric_retry
        from qobuz_librarian.library import new_releases, scan_checkpoint
        from qobuz_librarian.ui_cli.prompts import _read_fetch_log
        # These read state files and may submit a background job, so they run
        # here (off the event loop) alongside the other disk work. First-scan
        # first (establishes the baseline); the check is gated on that baseline.
        _maybe_auto_first_scan()
        _maybe_auto_check_new_releases()
        return {
            "new_release_review": _new_release_review(),
            # First-run setup banner: shown until a full library scan has seeded
            # the new-release baseline. setup_scanning = a library scan is now
            # pending/running — re-queried here (not from the pre-trigger
            # active_jobs) so the scan the auto-trigger just submitted shows.
            "baseline_complete": new_releases.is_baseline_complete(),
            "setup_scanning": _active_library_scan() is not None,
            "scan_resumable": scan_checkpoint.pending() is not None,
            # tail-only read so a long-running install with a multi-MB fetch log
            # doesn't slurp the whole file on every dashboard load.
            "recent": list(reversed(_read_fetch_log(limit_tail=8))),
            # First-run nudge: a fresh install has no creds, so every search/scan
            # would fail cryptically — surface it up front. Filesystem-only.
            "creds_ok": bool(_read_creds().get("auth_token")),
            "lyric_retry_count":
                len(load_lyric_retry()) if _cfg.LYRIC_RETRY_FILE.exists() else 0,
            "staging_album_count": 0 if active_jobs else _staging_album_count(),
            "last_library_scan": _last_scan_age(),
            "last_new_release_check": _last_new_release_check_age(),
        }

    loop = asyncio.get_running_loop()
    disk = await loop.run_in_executor(None, _gather_disk_state)
    return _tr(request, "index.html", {
        "active_jobs": active_jobs,
        "pending": job_mgr.registry.pending_and_running(),
        "review": job_mgr.registry.awaiting_review(),
        "creds_token_valid": _TOKEN_VALID,
        "lock_busy_pid": _LOCK_BUSY_PID,
        "page": "dashboard",
        **disk,
    })


@app.post("/lyric-retry")
async def lyric_retry(request: Request):
    # No credential check: lyric fetching only reads/writes local files and
    # talks to the lyric providers, never Qobuz.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    # A retry and a full backfill share the one lyric-state file, so they must
    # never run at once — fold onto whichever lyrics pass is already in flight.
    existing = _active_scan("lyrics", statuses=("pending", "running"))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Lyric retry")
    job.execute_kind = "lyrics"
    job_mgr.submit(job, lambda j: flows.run_lyric_retry(j, _get_token()))
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = Query("", max_length=500),
                      kind: str = Query("album")):
    if q.strip():
        return await do_search(request, q=q, kind=kind)
    creds_ok = bool(_read_creds().get("auth_token"))
    return _tr(request, "search.html", {
        "q": "", "results": [], "error": None,
        "kind": "track" if kind == "track" else "album",
        "creds_ok": creds_ok, "page": "search",
    })


@app.post("/search", response_class=HTMLResponse)
async def do_search(request: Request, q: str = Form("", max_length=500),
                    kind: str = Form("album")):
    results = []
    error = None
    query = q.strip()
    kind = "track" if str(kind).strip().lower() == "track" else "album"
    if query:
        # Imported before the try so the except clauses below can always name
        # them, even if a failure happens before the request reaches the API.
        from qobuz_librarian.api.auth import AuthLost, QobuzError, QobuzUnavailable
        try:
            token = _get_token()
            from qobuz_librarian.api.search import (
                get_album,
                get_track,
                search_albums,
                search_tracks,
            )
            from qobuz_librarian.cli import parse_qobuz_url
            from qobuz_librarian.library.catalog import album_quality_label, album_year

            # If the user pasted a Qobuz URL, the placeholder says we
            # handle it — actually do so by fetching the album directly
            # instead of doing a text search on the URL string.
            try:
                _split = urllib.parse.urlsplit(query)
                is_qobuz_url = (_split.scheme in ("http", "https")
                                and _split.netloc.lower().endswith("qobuz.com"))
            except ValueError:
                is_qobuz_url = False
            parsed = parse_qobuz_url(query) if is_qobuz_url else None
            raw = []
            loop = asyncio.get_running_loop()
            from qobuz_librarian.api.client import call_within
            if parsed and parsed[0] == "album" and kind == "track":
                # An album URL only resolves in Albums mode; in Tracks mode it
                # would fetch the album and then be dropped as not-a-track,
                # leaving a blank "No results". Point the user at the toggle.
                error = ("That's an album URL — switch to Albums to download it, "
                         "or paste a single track to grab one song.")
            elif parsed and parsed[0] == "album":
                try:
                    raw = [await asyncio.wait_for(
                        loop.run_in_executor(
                            None, lambda: call_within(
                                cfg.WEB_FETCH_TIMEOUT, get_album, parsed[1], token)
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )]
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
                except (AuthLost, QobuzUnavailable):
                    raise
                except QobuzError:
                    error = "Couldn't fetch that album — check the URL."
                except Exception:
                    import logging
                    logging.getLogger("qobuz_librarian").exception(
                        "album fetch failed for %r", query)
                    error = "Couldn't fetch that album — check the URL."
            elif parsed and parsed[0] == "track" and kind == "track":
                # Tracks mode: resolve the pasted track URL to that one track —
                # the track-results loop below then renders it for a one-song
                # grab (this is exactly what Tracks mode exists for).
                try:
                    _t = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: call_within(
                            cfg.WEB_FETCH_TIMEOUT, get_track, parsed[1], token)),
                        timeout=cfg.WEB_FETCH_TIMEOUT)
                    raw = [_t] if _t else []
                    if not raw:
                        error = "Couldn't fetch that track — check the URL."
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."
                except (AuthLost, QobuzUnavailable):
                    raise
                except QobuzError:
                    error = "Couldn't fetch that track — check the URL."
            elif parsed and parsed[0] == "track":
                # Albums mode: a track URL — point the user at the Tracks toggle
                # instead of the old (now false) "works on albums" message.
                error = ("That's a track URL — switch to Tracks to grab one "
                         "song, or paste the album URL in Albums mode.")
            elif parsed:
                # Parsed as some other Qobuz URL kind (artist/playlist).
                error = ("Only Qobuz album URLs are supported here. "
                         "Paste an album URL, or search by name.")
            elif is_qobuz_url:
                # URL looks like qobuz.com but isn't a recognised format
                # (e.g. artist/interpreter or playlist page). Text-searching
                # the URL string returns nothing and confuses the user.
                error = ("Only Qobuz album URLs are supported here. "
                         "Paste an album URL, or search by artist/title.")
            else:
                _search_fn = search_tracks if kind == "track" else search_albums
                try:
                    raw = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: call_within(cfg.WEB_FETCH_TIMEOUT, _search_fn,
                                                query, token, limit=cfg.SEARCH_LIMIT),
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."

            for t in (raw if kind == "track" else []):
                alb = t.get("album") or {}
                if not t.get("id") or not alb.get("id"):
                    continue
                _tbd = (t.get("maximum_bit_depth")
                        or alb.get("maximum_bit_depth") or 0)
                _timg = alb.get("image") or {}
                _tcover = _timg.get("small") or _timg.get("thumbnail") or ""
                _perf = (t.get("performer") or {}).get("name")
                results.append({
                    "track_id":    t.get("id"),
                    "album_id":    alb.get("id"),
                    "title":       t.get("title") or "?",
                    "version":     t.get("version") or "",
                    "artist":      (alb.get("artist") or {}).get("name") or _perf or "?",
                    "album_title": alb.get("title") or "?",
                    "year":        album_year(alb) or "?",
                    "track_n":     t.get("track_number") or "?",
                    "total":       alb.get("tracks_count") or "?",
                    "hires":       _tbd >= 24,
                    "lossy":       _tbd == 0,
                    "cover":       _tcover if _tcover.startswith(
                        "https://static.qobuz.com/") else "",
                })
            for a in (raw if kind == "album" else []):
                if not a.get("id"):
                    continue
                _bd = a.get("maximum_bit_depth") or 0
                _img = a.get("image") or {}
                _cover = _img.get("small") or _img.get("thumbnail") or ""
                # The Hi-Res/CD/Lossy badge beside this already names the tier,
                # so drop the repeated descriptor and keep just the bit/rate.
                _qual = album_quality_label(a).replace(" (hi-res)", "")
                if _qual == "lossy":
                    _qual = ""
                results.append({
                    "id":      a.get("id"),
                    "title":   a.get("title") or "?",
                    "artist":  (a.get("artist") or {}).get("name") or "?",
                    "year":    album_year(a) or "?",
                    "tracks":  a.get("tracks_count") or "?",
                    "quality": _qual,
                    "hires":   _bd >= 24,
                    "lossy":   _bd == 0,
                    "cover":   _cover if _cover.startswith(
                        "https://static.qobuz.com/") else "",
                })
        except (SystemExit, NoCredsError):
            error = "No Qobuz credentials set — visit Settings."
        except AuthLost:
            error = "Qobuz auth lost. Check Settings."
        except QobuzUnavailable:
            error = ("Qobuz is temporarily unavailable (network or rate "
                     "limit) — try again shortly.")
        except QobuzError:
            error = "Search failed — try again."
        except Exception:
            import logging
            logging.getLogger("qobuz_librarian").exception(
                "search failed for %r", query)
            error = "Search failed — try again."
    creds_ok = bool(_read_creds().get("auth_token"))
    ctx = {"q": query, "results": results, "error": error, "kind": kind,
           "creds_ok": creds_ok, "page": "search"}
    if _is_htmx(request):
        return _tr(request, "_search_results.html", ctx)
    return _tr(request, "search.html", ctx)


_DOWNLOAD_SUMMARY_LABELS = {
    "already_complete": "Album already complete — nothing to download.",
    "skipped_already_higher_quality": "Skipped — you already own higher quality.",
    "skipped_has_extras": "Skipped — your copy has tracks the catalogue doesn't.",
    "upgrade_only_no_op": "Already at or above the target quality.",
    "dry_run": "Dry run — nothing downloaded.",
    "user_skipped": "Skipped at confirmation.",
    "lossy_only": "Qobuz only had lossy versions — nothing downloaded.",
    "no_tracks": "Qobuz returned no tracks for this album.",
    "cancelled": "Cancelled — the partial download was discarded.",
    "upgrade_aborted_backup_failed": "Upgrade aborted — couldn't back up the original.",
    "partial": "Re-download came back incomplete — kept your original.",
    "not_imported": "Downloaded, but the import didn't land — library unchanged.",
}


def _summarize_download_result(r):
    """One-line job summary from process_album's result dict.

    Picks a phrase per result kind for the documented non-success branches,
    or builds the "N tracks downloaded" tally for an actual rip. Returns
    "" if there's nothing useful to say (process_album returned None / {})."""
    from qobuz_librarian.ui_cli.errors import plural

    if not r:
        return ""
    kind = r.get("result")
    if kind in _DOWNLOAD_SUMMARY_LABELS:
        return _DOWNLOAD_SUMMARY_LABELS[kind]
    if not r.get("imported"):
        return ""
    n_ok = r.get("n_ok", 0)
    n_fail = r.get("n_fail", 0)
    n_lossy = r.get("n_lossy", 0)
    parts = [f"{plural(n_ok, 'track')} downloaded"]
    if n_fail:
        parts.append(f"{n_fail} failed")
    if n_lossy:
        parts.append(f"{n_lossy} lossy-dropped")
    if r.get("upgrade_unverified"):
        parts.append("upgrade couldn't be verified — original kept")
    elif r.get("auto_upgrade"):
        parts.append("auto-upgrade verified")
    return ", ".join(parts) + "."


def _make_download_run(album, token, *, treat_as_new=False):
    """Return the run(j) callable used by both queue_download and job_retry.

    treat_as_new downloads the album as a brand-new one even if a different
    edition is already owned — the "get this edition too" path.
    """
    def run(j):
        from qobuz_librarian.modes.process import process_album
        from qobuz_librarian.ui_cli.errors import plural
        from qobuz_librarian.web.flows import build_args
        with job_mgr.staging_lock():
            r = process_album(album, build_args(), allow_force=False,
                              already_confirmed=True, token=token,
                              treat_as_new=treat_as_new) or {}
        benign = {"already_complete", "skipped_already_higher_quality",
                  "skipped_has_extras", "dry_run", "user_skipped",
                  "lossy_only", "no_tracks", "cancelled"}
        if r.get("result") not in benign and not r.get("imported"):
            j.status = job_mgr.JobStatus.FAILED
            j.error = (f"{plural(r['n_fail'], 'track')} failed"
                       if r.get("n_fail") else "download or import failed")
        elif r.get("imported") and r.get("n_fail", 0) > 0:
            j.error = f"{plural(r['n_fail'], 'track')} failed — see job log"
        # A successful job page used to show a blank summary line: the user
        # couldn't tell what happened from the /jobs page without expanding
        # the log. Surface a one-line outcome here.
        summary = _summarize_download_result(r)
        if summary:
            j.summary = summary
        # Claiming/completing the album the normal way graduates it out of the
        # "grabbed single" state, so the rest stops being suppressed in scans.
        if r.get("imported"):
            from qobuz_librarian.library import hidden as hidden_mod
            hidden_mod.unmark_single(
                (album.get("artist") or {}).get("name") or "?",
                album.get("title") or "?")
    return run


def _make_single_track_run(album, track, token):
    """Run a single-track grab: download just ``track`` via the per-track queue
    path (the same isolation repair uses — never a whole-album rip), then mark
    the album as a deliberately-grabbed single so scans leave the partial folder
    — and a sample-only artist's catalogue — alone."""
    def run(j):
        from qobuz_librarian.library import hidden as hidden_mod
        from qobuz_librarian.library.catalog import (
            album_year,
            compute_missing,
            find_existing_tracks,
        )
        from qobuz_librarian.queue.builder import _build_queue_item
        from qobuz_librarian.queue.executor import _execute_download_queue
        from qobuz_librarian.ui_cli.errors import plural
        from qobuz_librarian.web.flows import build_args
        artist = (album.get("artist") or {}).get("name") or "?"
        title = album.get("title") or "?"
        t_title = track.get("title") or "?"
        qobuz_tracks = (album.get("tracks") or {}).get("items") or []
        existing, album_dir = find_existing_tracks(album)
        missing, _present = compute_missing(qobuz_tracks, existing)
        missing_ids = {str(t.get("id")) for t in missing}
        # Already own this exact track? Don't re-rip it — that just lands a beets
        # ".1.flac" duplicate beside the copy you have — and don't mark anything.
        if str(track.get("id")) not in missing_ids:
            j.summary = f"You already have “{t_title}” — nothing downloaded."
            return
        qi = _build_queue_item(
            album=album, album_dir=album_dir,
            label=f"{artist} — {t_title}  [single]",
            missing=[track], present=existing,
            upgrade_only=False, auto_upgrade=False,
            force_track_by_track=True,
        )
        with job_mgr.staging_lock():
            _execute_download_queue([qi], build_args(), token)
        if not (qi.get("n_ok", 0) > 0 and qi.get("imported", False)
                and qi.get("n_fail", 0) == 0):
            j.status = job_mgr.JobStatus.FAILED
            j.error = (f"{plural(qi.get('n_fail', 1), 'track')} failed"
                       if qi.get("n_fail") else "download or import failed")
            return
        # Only mark it a single if the album is still partial after this grab. If
        # this was the album's last missing track, you now own the whole thing —
        # that's a normal complete album, not a single, so leave it unmarked.
        marked = len(missing) > 1
        if marked:
            hidden_mod.mark_single(artist, title, album_year(album), album.get("id"))
            j.summary = (f"Got “{t_title}” — filed under {artist} / {title}. "
                         "The rest of the album stays out of your scans.")
        else:
            # This grab completed the album — it's a normal full album now, so
            # clear any single mark an earlier partial grab of it left behind.
            # Without this the stale mark keeps the artist out of bulk scans and
            # the new-release check even though nothing is partial any more.
            hidden_mod.unmark_single(artist, title)
            j.summary = (f"Got “{t_title}” — that completed {title}, so it's "
                         "filed as a full album.")
        # Record what this grab added so /undo can cleanly reverse it.
        j.single = {
            "album_id": str(album.get("id") or ""),
            "track_id": str(track.get("id") or ""),
            "dir": qi.get("_resolved_post_dir") or (str(album_dir) if album_dir else ""),
            "isrc": track.get("isrc") or "",
            "track_no": track.get("track_number"),
            "title": t_title, "artist": artist, "album": title,
            "marked": marked, "new_folder": album_dir is None,
        }
    return run


@app.post("/download", response_class=HTMLResponse)
async def queue_download(request: Request, album_id: str = Form(""),
                         as_new_edition: str = Form(""),
                         track_id: str = Form("")):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    album_id = album_id.strip()
    track_id = track_id.strip()
    if not album_id:
        msg = "Missing album id."
        if _is_htmx(request):
            # 200, not 400: htmx only swaps 2xx/3xx responses, so a 400 fragment
            # is silently dropped and the user sees no feedback. The alert-error
            # styling carries the "this failed" meaning instead of the status.
            return HTMLResponse(
                f'<div class="alert alert-error" data-flash>{msg}</div>')
        return RedirectResponse(url="/queue?error=" + urllib.parse.quote(msg),
                                status_code=303)
    # Refuse duplicates — same album already active or pending. Includes
    # scan-flow jobs awaiting review that have the album as one of their
    # candidates, so a search-then-download for an album the user just
    # approved in an artist scan still hits the dedupe guard.
    existing = _find_job_touching_album(album_id)
    if existing:
        if _is_htmx(request):
            return HTMLResponse(
                f'<div class="alert alert-warning" data-flash>Already queued — '
                f'<a href="/jobs/{existing.id}" class="link">view job</a>.</div>')
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    # "Get this edition too" — download a different edition of an album the user
    # already owns, as a separate album. Bypasses the owned-check and treats it
    # as brand-new so it lands in its own (year) folder beside the existing copy
    # rather than being skipped or replacing it.
    download_as_new_edition = str(as_new_edition).strip().lower() in (
        "1", "true", "yes", "on")
    try:
        token = _get_token()
        from qobuz_librarian.api.client import call_within
        from qobuz_librarian.api.search import get_album
        loop = asyncio.get_running_loop()
        album = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: call_within(cfg.WEB_FETCH_TIMEOUT, get_album, album_id, token)),
            timeout=cfg.WEB_FETCH_TIMEOUT,
        )
        if not download_as_new_edition and not track_id:
            def _already_complete():
                from qobuz_librarian.library.catalog import (
                    compute_missing,
                    find_album_dir_filesystem,
                    find_existing_tracks,
                )
                try:
                    album_dir = find_album_dir_filesystem(album)
                except Exception:
                    return False
                if album_dir is None:
                    return False
                try:
                    # Already resolved above; pass it through so we don't repeat
                    # the cached-subdir scan + fuzzy fallback for the same album.
                    existing_tracks, _ = find_existing_tracks(album, album_dir=album_dir)
                except Exception:
                    existing_tracks = []
                qobuz_tracks = (album.get("tracks") or {}).get("items") or []
                # Only count it complete when nothing's missing. A partial album
                # (some present, some missing) returns False so process_album can
                # gap-fill the missing tracks instead of forcing a full re-rip.
                return bool(existing_tracks and qobuz_tracks) and not (
                    compute_missing(qobuz_tracks, existing_tracks)[0])

            # Resolving the album folder walks the (often NAS-mounted) library,
            # so keep it off the event loop — otherwise a large library stalls
            # every other request while this one request blocks.
            if await loop.run_in_executor(None, _already_complete):
                msg = "You already own a version of this album."
                if _is_htmx(request):
                    # Offer the deliberate second-edition path instead of a dead
                    # end: a remaster or a different mix can be kept alongside the
                    # owned copy (it imports into its own (year) folder). The form
                    # re-posts with as_new_edition so the owned-check is skipped.
                    aid = html.escape(album_id)
                    return HTMLResponse(
                        f'<div class="alert alert-warning">'
                        f'<div><p>{html.escape(msg)}</p>'
                        f'<p class="text-xs text-base-content/60 mt-1">A different '
                        f'edition — a remaster or a new mix — can be kept alongside '
                        f'it; it downloads into its own folder. (If it shares the '
                        f'same release year as your copy, your player may merge '
                        f'them.)</p></div>'
                        f'<form hx-post="/download" hx-target="#download-toast" '
                        f'hx-swap="innerHTML" class="mt-2">'
                        f'<input type="hidden" name="album_id" value="{aid}">'
                        f'<input type="hidden" name="as_new_edition" value="1">'
                        f'<button type="submit" class="btn btn-sm btn-primary">'
                        f'Download this edition too</button></form></div>')
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(msg),
                    status_code=303)
        title  = album.get("title") or "?"
        artist = (album.get("artist") or {}).get("name") or "?"
        single_track = None
        if track_id:
            _tracks = (album.get("tracks") or {}).get("items") or []
            single_track = next(
                (t for t in _tracks if str(t.get("id")) == track_id), None)
            if single_track is None:
                msg = "That track isn't on this album."
                if _is_htmx(request):
                    # 200, not 400: htmx drops non-2xx/3xx fragments, so a 400
                    # here renders nothing. alert-error conveys the failure.
                    return HTMLResponse(
                        f'<div class="alert alert-error" data-flash>{msg}</div>')
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(msg), status_code=303)
        job = job_mgr.Job(
            title=(single_track.get("title") or title) if single_track else title,
            artist=artist, album_id=album_id)
        if single_track:
            # Flagging it now (before the run fills in the undo details) is what
            # tells the UI to hide Cancel on this job — a one-track grab is done
            # before you could catch it.
            job.single = {"album_id": album_id, "track_id": str(track_id)}

        # Re-check under the lock right before submitting: closes the race with
        # a concurrent /download for the same album across the get_album await.
        with _DOWNLOAD_SUBMIT_LOCK:
            dup = _find_job_touching_album(album_id)
            if dup:
                if _is_htmx(request):
                    return HTMLResponse(
                        f'<div class="alert alert-warning" data-flash>Already queued — '
                        f'<a href="/jobs/{dup.id}" class="link">view job</a>.</div>')
                return RedirectResponse(url=f"/jobs/{dup.id}", status_code=303)
            # Re-check the run-lock right before submitting. The album fetch
            # above awaited, and set_mode could have handed the lock to the
            # terminal in that window — while this not-yet-registered job was
            # invisible to set_mode's active-job check, so it wouldn't have
            # refused the handoff. There's no await between here and submit, so
            # this read and the registry add are atomic on the event loop: once
            # the job is registered the handoff sees it and is refused instead.
            busy = _lock_busy_response(request)
            if busy is not None:
                return busy
            run_fn = (_make_single_track_run(album, single_track, token)
                      if single_track
                      else _make_download_run(
                          album, token, treat_as_new=download_as_new_edition))
            job_mgr.submit(job, run_fn)
        if _is_htmx(request):
            return _tr(request, "_job_queued.html", {"job": job})
        # Land on the new job's page so the user sees their download starting.
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    except (SystemExit, NoCredsError):
        msg = "No Qobuz credentials set — visit Settings."
        if _is_htmx(request):
            return HTMLResponse(
                f'<div class="alert alert-error" data-flash>{msg}</div>')
        return RedirectResponse(url="/settings?error=creds", status_code=303)
    except Exception as e:
        from qobuz_librarian.api.auth import (
            AuthLost,
            QobuzError,
            QobuzUnavailable,
            friendly_qobuz_error,
        )
        if isinstance(e, asyncio.TimeoutError):
            user_msg = "Timed out reaching the Qobuz API — try again."
        elif isinstance(e, QobuzUnavailable):
            user_msg = ("Qobuz is temporarily unavailable (network or rate "
                        "limit) — try again shortly.")
        elif isinstance(e, AuthLost):
            user_msg = "Token is expired or invalid — update it in Settings."
        elif isinstance(e, QobuzError):
            cleaned = friendly_qobuz_error(e)
            if cleaned.startswith("HTTP 404"):
                user_msg = ("No album with that id — check the URL "
                            "or use Search.")
            else:
                user_msg = ("Couldn't reach the Qobuz API — "
                            "check the container's network.")
        else:
            user_msg = "Couldn't queue download — check your token and try again."
        if _is_htmx(request):
            return HTMLResponse(
                f'<div class="alert alert-error" data-flash>{html.escape(user_msg)}</div>')
        msg = urllib.parse.quote(user_msg, safe="")
        return RedirectResponse(url=f"/queue?error={msg}", status_code=303)


@app.get("/artist", response_class=HTMLResponse)
async def artist_page(request: Request, error: str = "", artist: str = ""):
    creds_ok = bool(_read_creds().get("auth_token"))
    # `artist` pre-fills the box (e.g. the Hidden view's "check for new releases"
    # link). Strip angle brackets/NULs so it can't break out of the value attr.
    prefill = "".join(c for c in artist if c not in "<>\x00").strip()[:200]
    return _tr(request, "artist.html", {
        "creds_ok": creds_ok, "error": error[:200], "page": "artist",
        "prefill": prefill,
    })


def _clean_artist_name(artist):
    """Strip + length-cap + reject control chars. Returns (name, error_redirect).

    error_redirect is None on success, or a RedirectResponse back to the
    Artist page with a flash. Used by the artist scan + the 4 per-artist tool
    routes so they all reject the same way."""
    name = (artist or "").strip()[:200]
    if not name:
        return None, RedirectResponse(
            url="/artist?error=" + urllib.parse.quote("Artist name is required."),
            status_code=303,
        )
    if any(c in name for c in ("<", ">", "\x00")):
        return None, RedirectResponse(
            url="/artist?error=" + urllib.parse.quote(
                "Artist name contains forbidden characters."),
            status_code=303,
        )
    return name, None


@app.post("/artist")
async def artist_scan(request: Request, artist: str = Form("")):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    name, err = _clean_artist_name(artist)
    if err is not None:
        return err
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Artist scan", artist=name)
    job.execute_kind = "album"
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_artist(j, name, _get_token()),
        lambda j, chosen: flows.execute_albums(j, chosen, _get_token()),
        "album")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    from qobuz_librarian.library import scan_checkpoint
    creds_ok = bool(_read_creds().get("auth_token"))
    # Resume hint — same pattern /repair uses: only surface when a checkpoint
    # exists AND no library scan is currently running (mid-scan the dashboard's
    # "scanning…" indicator already covers it).
    cp = scan_checkpoint.pending()
    library_resume = (cp if cp is not None and _active_scan("library") is None
                      else None)
    return _tr(request, "library.html", {
        "creds_ok": creds_ok, "page": "library",
        "library_resume": library_resume,
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_MISSING)})


@app.post("/library")
async def library_scan(request: Request, mode: str = Form("missing_albums")):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    mode_norm = (mode or "").strip().lower()
    # Run the submit off the event loop: it takes _auto_check_lock, which the
    # dashboard auto-triggers can hold across small data-volume reads, and the
    # loop shouldn't block on a (possibly NAS) mount — same reason the dashboard
    # does its disk work in an executor.
    loop = asyncio.get_running_loop()
    if mode_norm == "new_releases":
        # Same job the dashboard auto-check submits; its own execute_kind so the
        # review screen pre-ticks the new releases and labels the surface.
        job = await loop.run_in_executor(None, _start_new_release_check)
        if job is None:    # lock handed to the terminal during the submit
            return _lock_busy_response(request) or RedirectResponse(
                url="/settings?mode=cli", status_code=303)
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    # "library" (not "album") so the review screen knows this is the paced triage
    # surface; both modes run the same album executor and resume from a matching
    # checkpoint if one's waiting (see _start_library_scan / scan_library).
    job = await loop.run_in_executor(
        None, lambda: _start_library_scan(partial_only=(mode_norm == "partial_fill")))
    if job is None:
        return _lock_busy_response(request) or RedirectResponse(
            url="/settings?mode=cli", status_code=303)
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


def _hidden_view(request, scope, *, page, restore_action, back_url):
    from qobuz_librarian.library import hidden as hidden_mod
    return _tr(request, "hidden.html", {
        "page": page, "scope": scope, "back_url": back_url,
        "restore_action": restore_action,
        "groups": hidden_mod.hidden_by_artist(scope)})


async def _restore_hidden(request, scope, redirect):
    # Mutates the hidden store, so it honours the run-lock like every other
    # state-changing POST — a restore mustn't race a CLI run or another job.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.library import hidden as hidden_mod
    form = await request.form()
    artists = form.getlist("artist")[:10000]
    fingerprints = form.getlist("fingerprint")[:10000]
    if artists:
        hidden_mod.restore(scope, artists)
    if fingerprints:
        hidden_mod.restore_albums(scope, fingerprints)
    return RedirectResponse(url=redirect, status_code=303)


@app.get("/library/hidden", response_class=HTMLResponse)
async def library_hidden(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return _hidden_view(request, hidden_mod.SCOPE_MISSING, page="library",
                        restore_action="/library/hidden/restore", back_url="/library")


@app.post("/library/hidden/restore")
async def library_hidden_restore(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return await _restore_hidden(request, hidden_mod.SCOPE_MISSING, "/library/hidden")


@app.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    creds_ok = bool(_read_creds().get("auth_token"))
    return _tr(request, "upgrade.html", {
        "creds_ok": creds_ok, "page": "upgrade",
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_UPGRADE)})


@app.get("/upgrade/hidden", response_class=HTMLResponse)
async def upgrade_hidden(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return _hidden_view(request, hidden_mod.SCOPE_UPGRADE, page="upgrade",
                        restore_action="/upgrade/hidden/restore", back_url="/upgrade")


@app.post("/upgrade/hidden/restore")
async def upgrade_hidden_restore(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return await _restore_hidden(request, hidden_mod.SCOPE_UPGRADE, "/upgrade/hidden")


@app.post("/upgrade")
async def upgrade_scan(request: Request):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Quality upgrade scan")
    job.execute_kind = "upgrade"
    job.review_verb = "Upgrade"  # the action re-rips, not a fresh download
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_upgrades(j, _get_token()),
        lambda j, chosen: flows.execute_upgrades(j, chosen, _get_token()),
        "upgrade")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.post("/upgrade/artist")
async def upgrade_scan_artist(request: Request, artist: str = Form("")):
    """Scan one artist's library folder for quality upgrades. Same review-then-
    execute flow as the whole-library scan, scoped to one artist's albums.
    The Hidden-Upgrade store is deliberately NOT consulted here — asking for
    an artist by name is a 'show me everything for this artist' request, same
    convention scan_artist (find-missing) already followed."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    name, err = _clean_artist_name(artist)
    if err is not None:
        return err
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    from qobuz_librarian.web import flows
    # title is the scan kind only; the job-page header prepends `artist` already
    # (the existing /artist library scan follows the same convention).
    job = job_mgr.Job(title="Quality upgrade scan", artist=name)
    job.execute_kind = "upgrade"
    job.review_verb = "Upgrade"
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_upgrades_for_artist(j, name, _get_token()),
        lambda j, chosen: flows.execute_upgrades(j, chosen, _get_token()),
        "upgrade")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/downsample", response_class=HTMLResponse)
async def downsample_page(request: Request):
    from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE
    from qobuz_librarian.library import hidden as hidden_mod
    # creds_ok gates the "Just one artist?" hint — /artist hides its whole
    # form when no Qobuz creds are set, so promising a per-artist downsample
    # there would be a dead link for a no-creds user (downsample itself is
    # credential-free, so they CAN still use the whole-library button here).
    return _tr(request, "downsample.html", {
        "page": "downsample",
        "have_downsample": HAVE_DOWNSAMPLE,
        "creds_ok": bool(_read_creds().get("auth_token")),
        "hidden_count": hidden_mod.count(hidden_mod.SCOPE_DOWNSAMPLE)})


@app.get("/downsample/hidden", response_class=HTMLResponse)
async def downsample_hidden(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return _hidden_view(request, hidden_mod.SCOPE_DOWNSAMPLE, page="downsample",
                        restore_action="/downsample/hidden/restore",
                        back_url="/downsample")


@app.post("/downsample/hidden/restore")
async def downsample_hidden_restore(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    return await _restore_hidden(request, hidden_mod.SCOPE_DOWNSAMPLE,
                                 "/downsample/hidden")


@app.post("/downsample")
async def downsample_scan(request: Request):
    # No credential check: downsampling only reads and rewrites local files.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Downsample scan")
    job.execute_kind = "downsample"
    job.review_verb = "Downsample"  # the action rewrites files, not a download
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_downsamples(j),
        lambda j, chosen: flows.execute_downsamples(j, chosen),
        "downsample")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.post("/downsample/artist")
async def downsample_scan_artist(request: Request, artist: str = Form("")):
    """Scan one artist's library folder for hi-res files. Local-only — no
    Qobuz creds needed (the answer comes off disk)."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    name, err = _clean_artist_name(artist)
    if err is not None:
        return err
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Downsample scan", artist=name)
    job.execute_kind = "downsample"
    job.review_verb = "Downsample"
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_downsamples_for_artist(j, name),
        lambda j, chosen: flows.execute_downsamples(j, chosen),
        "downsample")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/repair", response_class=HTMLResponse)
async def repair_page(request: Request):
    from qobuz_librarian.library import scan_checkpoint
    creds_ok = bool(_read_creds().get("auth_token"))
    # Surface a resume only for a genuinely interrupted sweep — not one whose
    # periodic checkpoint exists because a repair scan is running right now.
    cp = scan_checkpoint.load("repair")
    resume = (None if cp is None or _active_scan("repair") is not None
              else {"done": len(cp["scanned"]), "found": len(cp["candidates"])})
    return _tr(request, "repair.html",
               {"creds_ok": creds_ok, "page": "repair", "repair_resume": resume})


@app.post("/repair")
async def repair_scan(request: Request):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Repair scan")
    job.execute_kind = "repair"
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_repairs(j, _get_token()),
        lambda j, chosen: flows.execute_repairs(j, chosen, _get_token()),
        "repair")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.post("/repair/artist")
async def repair_scan_artist(request: Request, artist: str = Form("")):
    """Scan one artist's albums for ISRC-verified truncated FLACs. No
    checkpoint here (the focused single-artist run is fast); the whole-library
    sweep keeps its checkpoint because it can run for hours."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    name, err = _clean_artist_name(artist)
    if err is not None:
        return err
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Repair scan", artist=name)
    job.execute_kind = "repair"
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_repairs_for_artist(j, name, _get_token()),
        lambda j, chosen: flows.execute_repairs(j, chosen, _get_token()),
        "repair")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/repair/history", response_class=HTMLResponse)
async def repair_history(request: Request):
    """Show what Repair has refilled in place — so the user knows which albums
    to refresh on an offline-sync client that may still serve the old broken
    file. The log itself is append-only on disk (DATA_DIR); this is read-only."""
    from qobuz_librarian.repair_log import read_repair_log_entries
    # Walks lines on the data volume — offload to match the dashboard's pattern
    # and keep the event loop free if the file is sizable.
    loop = asyncio.get_running_loop()
    entries = await loop.run_in_executor(
        None, lambda: read_repair_log_entries(limit=500))
    return _tr(request, "repair_history.html",
               {"page": "repair", "entries": entries})


@app.get("/lyrics", response_class=HTMLResponse)
async def lyrics_page(request: Request):
    from qobuz_librarian.integrations.lyric_fetch import AVAILABLE
    providers = ", ".join(cfg.LYRICS_PROVIDERS) or "Lrclib, NetEase, Musixmatch"
    # creds_ok gates the "Just one artist?" hint — see downsample_page for the
    # full story: /artist hides its form without creds, so the hint would be
    # a dead link there for a no-creds user.
    return _tr(request, "lyrics.html", {
        "page": "lyrics",
        "have_lyrics": AVAILABLE,
        "creds_ok": bool(_read_creds().get("auth_token")),
        "lyrics_format": (cfg.LYRICS_FORMAT or "embed").lower(),
        "providers": providers,
    })


@app.post("/lyrics")
async def lyrics_scan(request: Request):
    # No credential check: lyric fetching only reads/writes local files and
    # talks to the lyric providers, never Qobuz.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    form = await request.form()
    rescan = bool(form.get("rescan"))
    synced_only = bool(form.get("synced_only"))
    existing = _active_scan("lyrics", statuses=("pending", "running"))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Lyrics backfill")
    job.execute_kind = "lyrics"
    job_mgr.submit(
        job,
        lambda j: flows.run_library_lyrics(j, rescan=rescan, synced_only=synced_only),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.post("/lyrics/artist")
async def lyrics_scan_artist(request: Request, artist: str = Form("")):
    """Fetch lyrics for one artist's library tracks only. Same state file as
    the whole-library run, so this still skips tracks an earlier run resolved."""
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    name, err = _clean_artist_name(artist)
    if err is not None:
        return err
    form = await request.form()
    rescan = bool(form.get("rescan"))
    synced_only = bool(form.get("synced_only"))
    existing = _active_scan("lyrics", statuses=("pending", "running"))
    if existing is not None:
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Lyrics backfill", artist=name)
    job.execute_kind = "lyrics"
    job_mgr.submit(
        job,
        lambda j: flows.run_lyrics_for_artist(
            j, name, rescan=rescan, synced_only=synced_only),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


def _migrate_checks(src, dest):
    import os

    from qobuz_librarian.library.migrate import _existing_ancestor
    checks = []
    for label, path in (("Source folder", src), ("Destination folder", dest)):
        if not path:
            checks.append({"label": label, "ok": False, "detail": "not set"})
            continue
        p = Path(path)
        is_dest = label.startswith("Destination")
        if not p.exists():
            # The migration creates the destination tree, so a not-yet-created
            # dest is fine as long as a writable ancestor exists to land it in.
            anc = _existing_ancestor(p) if is_dest else None
            if is_dest and anc and os.access(str(anc), os.W_OK):
                checks.append({"label": label, "ok": True,
                               "detail": f"{p} (will be created under {anc})"})
            elif is_dest:
                checks.append({"label": label, "ok": False,
                               "detail": f"{p} can't be created — nearest existing "
                                         f"folder {anc or p.anchor} is not writable"})
            else:
                checks.append({"label": label, "ok": False, "detail": f"{p} does not exist"})
        elif not p.is_dir():
            checks.append({"label": label, "ok": False, "detail": f"{p} is not a directory"})
        elif not os.access(str(p), os.R_OK):
            checks.append({"label": label, "ok": False, "detail": f"{p} is not readable"})
        elif is_dest and not os.access(str(p), os.W_OK):
            checks.append({"label": label, "ok": False, "detail": f"{p} is not writable"})
        else:
            checks.append({"label": label, "ok": True, "detail": str(p)})
    return checks


@app.get("/migrate", response_class=HTMLResponse)
async def migrate_page(request: Request):
    src, dest = cfg.MIGRATE_SRC, cfg.MIGRATE_DEST
    return _tr(request, "migrate.html", {
        "page": "migrate",
        "src": src,
        "dest": dest,
        "configured": bool(src and dest),
        "migrate_checks": _migrate_checks(src, dest),
    })


@app.post("/migrate")
async def migrate_scan(request: Request):
    # No credential check: migration only reads and reorganizes local files.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.library import migrate as engine
    src, dest = cfg.MIGRATE_SRC, cfg.MIGRATE_DEST
    form = await request.form()
    use_acoustid = form.get("acoustid") == "on"
    in_place = form.get("in_place") == "on"
    if not src or not dest:
        err = ("Set QL_MIGRATE_SRC and QL_MIGRATE_DEST — the folder to read and "
               "the folder to build the organized copy into — then try again.")
    else:
        err = engine.validate_paths(Path(src), Path(dest), in_place=in_place)
    if err:
        return _tr(request, "migrate.html", {
            "page": "migrate", "src": src, "dest": dest,
            "configured": bool(src and dest), "error": err,
            "migrate_checks": _migrate_checks(src, dest)})
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Library migration")
    job.review_verb = "Move" if in_place else "Copy"
    job.execute_kind = "migration"
    # src is persisted so a resume after restart can still prune the emptied
    # source folders on an in-place move (the live execute below gets it too).
    job.execute_args = {"dest": str(dest), "in_place": bool(in_place),
                        "src": str(src)}
    job = await _submit_scan_deduped_async(
        job,
        lambda j: flows.scan_migration(j, src, dest, use_acoustid=use_acoustid,
                                       in_place=in_place),
        lambda j, chosen: flows.execute_migration(j, chosen, dest,
                                                  in_place=in_place, src=src),
        "migration")
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


# Backwards-compatible /audit redirects, in case anyone bookmarked the
# old name during the early-access period. Safe to remove later.
@app.get("/audit", response_class=HTMLResponse)
async def audit_redirect():
    return RedirectResponse(url="/repair", status_code=308)


@app.post("/audit")
async def audit_redirect_post():
    return RedirectResponse(url="/repair", status_code=308)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str, approved: bool = False,
                   stale: bool = False, page: int = 1):
    job = job_mgr.registry.get(job_id)
    historical = False
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return RedirectResponse(url="/queue", status_code=303)
        historical = True
    ctx = {"job": job, "page": "queue",
           "approved": approved, "stale": stale,
           "historical": historical,
           "JobStatus": job_mgr.JobStatus}
    ctx.update(_review_context(job, page))
    return _tr(request, "job.html", ctx)


def _review_context(job, page=1, query=""):
    """Template vars for a paginated awaiting-review body: the current page's
    artist groups, the page number/count, and the authoritative whole-set
    counts. Cheap no-op for non-review states (no candidates → one empty page).
    """
    from qobuz_librarian.ui_cli.colors import format_size
    groups = _review_artist_groups(job, query)
    page_groups, page, n_pages = _paginate_groups(groups, page)
    counts = job.selection_counts()
    return {
        "review_groups": page_groups,
        "review_page": page,
        "review_pages": n_pages,
        "review_query": query,
        "review_counts": counts,
        "review_reclaimable_label": (format_size(counts["reclaimable"])
                                     if counts["reclaimable"] else ""),
        "review_page_size": REVIEW_PAGE_ARTISTS,
    }


@app.get("/jobs/{job_id}/content", response_class=HTMLResponse)
async def job_content(request: Request, job_id: str, page: int = 1):
    """The job page's state-specific body, on its own. The live page swaps
    this in when the SSE stream reports the job finished, so the terminal
    view has one render path — the server's — instead of a faked-up bar."""
    job = job_mgr.registry.get(job_id)
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return HTMLResponse("", status_code=404)
    ctx = {"job": job, "JobStatus": job_mgr.JobStatus}
    ctx.update(_review_context(job, page))
    return _tr(request, "_job_body.html", ctx)


@app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
async def job_review_page(request: Request, job_id: str, page: int = 1,
                          q: str = ""):
    """One page of the paginated review list (groups + pager + summary), for
    Prev/Next and the whole-set artist filter. Rendered from saved selection
    flags, so ticks persist and span pages."""
    job = job_mgr.registry.get(job_id)
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return HTMLResponse("", status_code=404)
    ctx = {"job": job, "JobStatus": job_mgr.JobStatus}
    ctx.update(_review_context(job, page, q))
    return _tr(request, "_review_page.html", ctx)


@app.post("/jobs/{job_id}/approve")
async def job_approve(request: Request, job_id: str):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    job = job_mgr.registry.get(job_id)
    if not job:
        return RedirectResponse(url="/queue", status_code=303)
    # Selection is saved server-side as the user ticks (the paginated review no
    # longer carries every checkbox in the form), so approve runs against the
    # saved flags — passing None keeps them as-is rather than reading the form.
    # Offload to a thread: approve() does a json.dumps of up to JOB_CANDIDATE_CAP
    # candidate dicts + a SQLite commit, which would block the single event loop
    # (freezing every SSE stream / other request) for a large parked review —
    # the same reason /select was offloaded.
    loop = asyncio.get_running_loop()
    approved = await loop.run_in_executor(None, lambda: job_mgr.approve(job, None))
    flag = "approved=1" if approved else "stale=1"
    return RedirectResponse(url=f"/jobs/{job_id}?{flag}", status_code=303)


# Scan kinds that get the paced-triage surface (unticked, hideable, live-fill).
# Both share one review screen; they differ only in which hidden-store scope a
# dismiss writes — missing-album for the gap scan, upgrade for the upgrade scan.
_TRIAGE_KINDS = ("library", "upgrade", "new_releases", "downsample")

# Kinds whose review screen has server-backed per-candidate selection. Artist
# ("album") scans render the same checkboxes and approve from the saved flags,
# so their ticks must persist too — without "album" here every tick 404s and
# the user's edits never reach the server.
_SELECTABLE_KINDS = _TRIAGE_KINDS + ("repair", "migration", "album")


def _hide_scope(execute_kind):
    from qobuz_librarian.library import hidden as hidden_mod
    if execute_kind == "upgrade":
        return hidden_mod.SCOPE_UPGRADE
    if execute_kind == "downsample":
        return hidden_mod.SCOPE_DOWNSAMPLE
    return hidden_mod.SCOPE_MISSING


# Artist groups per review page. A huge gap scan can surface thousands of
# albums; rendering them all is what made the review page tank, so the server
# pages by whole artist groups (an album never splits across a page).
REVIEW_PAGE_ARTISTS = 25


def _artist_sort_key(name: str) -> str:
    """Order artists ignoring a leading article, so 'The Beatles' files under B
    (not T) and 'A Tribe Called Quest' under T — the way music libraries sort.
    Case-insensitive."""
    low = (name or "").strip().casefold()
    for art in ("the ", "a ", "an "):
        if low.startswith(art):
            return low[len(art):]
    return low


def _review_artist_groups(job, query=""):
    """Candidates grouped by artist for the review screen, in a deterministic
    order so pagination is stable across reloads. ``query`` filters across the
    WHOLE set (artist name or any album title), so the filter spans pages, not
    just the one on screen. Returns a list of (artist, items) pairs."""
    with job._lock:
        cands = list(job.candidates)
    q = (query or "").strip().lower()
    groups: dict = {}
    for c in cands:
        artist = c.get("artist") or ""
        if q:
            hay = artist + " " + (c.get("title") or "")
            if q not in hay.lower():
                continue
        groups.setdefault(artist, []).append(c)
    # Sort groups by artist (case-insensitive), tracks by their stable seq.
    ordered = []
    for artist in sorted(groups, key=lambda a: a.casefold()):
        items = sorted(groups[artist], key=lambda c: c.get("seq", 0))
        ordered.append((artist, items))
    return ordered


def _paginate_groups(groups, page):
    """Slice artist groups into one page. Returns (page_groups, page, n_pages).
    ``page`` is clamped into range so a stale/empty page lands somewhere valid."""
    n_pages = max(1, (len(groups) + REVIEW_PAGE_ARTISTS - 1) // REVIEW_PAGE_ARTISTS)
    page = max(1, min(int(page or 1), n_pages))
    start = (page - 1) * REVIEW_PAGE_ARTISTS
    return groups[start:start + REVIEW_PAGE_ARTISTS], page, n_pages


def _get_reviewable_job(job_id):
    """A job from the live registry, or rehydrated from disk if it has been
    evicted — so a restored/archived awaiting-review job's selection and pager
    keep working, not just the page render. Returns None if it's nowhere."""
    job = job_mgr.registry.get(job_id)
    if job is None:
        job = job_mgr.load_historical_job(job_id)
    return job


def _selection_payload(job):
    """JSON the selection/hide endpoints return so every open tab can refresh
    its counts from the server instead of recounting a partial DOM."""
    from qobuz_librarian.ui_cli.colors import format_size
    c = job.selection_counts()
    return {
        "selected": c["selected"],
        "total": c["total"],
        "artists": c["artists"],
        "reclaimable": c["reclaimable"],
        "reclaimable_label": format_size(c["reclaimable"]) if c["reclaimable"] else "",
    }


@app.post("/jobs/{job_id}/select")
async def job_select(request: Request, job_id: str):
    """Persist a single tick/untick. The review page no longer trusts the
    posted checkboxes (pagination means most aren't in the DOM), so each toggle
    saves immediately and the saved flags are the source of truth at download."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind not in _SELECTABLE_KINDS:
        return JSONResponse({"error": "not found"}, status_code=404)
    from qobuz_librarian.web import job_persistence
    form = await request.form()
    cid = (form.get("cid") or "").strip()
    on = (form.get("checked") or "").strip().lower() in ("1", "true", "on", "yes")
    if cid and job.set_selected(cid, on):
        # persist() json.dumps the whole candidates list (multi-MB near the
        # candidate cap) and writes SQLite under a lock — keep it off the event
        # loop so a single checkbox tick doesn't stall every other request.
        await asyncio.get_running_loop().run_in_executor(
            None, job_persistence.persist, job)
        job.notify_review_changed()
    return JSONResponse(_selection_payload(job))


@app.post("/jobs/{job_id}/select-all")
async def job_select_all(request: Request, job_id: str):
    """Bulk select/deselect. scope=all flips every candidate across all pages;
    scope=page flips only the cids posted (the visible page)."""
    job = _get_reviewable_job(job_id)
    if not job or job.execute_kind not in _SELECTABLE_KINDS:
        return JSONResponse({"error": "not found"}, status_code=404)
    from qobuz_librarian.web import job_persistence
    form = await request.form()
    on = (form.get("on") or "").strip().lower() in ("1", "true", "on", "yes")
    scope = (form.get("scope") or "all").strip().lower()
    cids = form.getlist("cid")[:100000] if scope == "page" else None
    if job.set_all_selected(on, cids=cids):
        await asyncio.get_running_loop().run_in_executor(
            None, job_persistence.persist, job)
        job.notify_review_changed()
    return JSONResponse(_selection_payload(job))


@app.post("/jobs/{job_id}/hide", response_class=HTMLResponse)
async def job_hide(request: Request, job_id: str):
    """Dismiss an artist's albums from a triage scan (gap or upgrade).

    A triage action, not a download — it writes the durable hidden-store (in
    the scan's scope) and drops those candidates from the review list,
    returning just the affected artist's group (or empty if the whole artist is
    gone) for an htmx swap of that one group. Allowed while the scan is still
    running, and never lock-guarded, so dismissing stays available mid-scan and
    while a download holds the staging lock.
    """
    # Use the disk fallback like every other review endpoint so Hide keeps
    # working on a restored/archived awaiting-review job (registry.get alone
    # 404s once the job is evicted, while /select, /review and /content don't).
    job = _get_reviewable_job(job_id)
    if not job:
        return HTMLResponse("", status_code=404)
    if (job.execute_kind in _TRIAGE_KINDS and job.status in (
            job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)):
        from qobuz_librarian.web import flows
        form = await request.form()
        artist = (form.get("artist") or "").strip()
        # Selection is server-backed, so hide keeps this artist's ticked albums
        # and drops the rest — no form keep-set, which under pagination would
        # only carry the visible page and clobber other pages' selections.
        n = flows.dismiss_albums(job, artist, scope=_hide_scope(job.execute_kind))
        if n:
            job.notify_review_changed()  # keep other open tabs in sync
        with job._lock:
            remaining = [c for c in job.candidates if c.get("artist") == artist]
        if remaining:
            resp = _tr(request, "_review_group.html",
                       {"job": job, "artist": artist, "items": remaining,
                        "triage": True, "open": True})
        else:
            resp = HTMLResponse("")  # whole artist hidden — outerHTML drops it
        if n:
            # Carry the fresh authoritative counts so the page updates the
            # summary/selected/reclaimable without recounting a partial DOM.
            import json as _json
            resp.headers["HX-Trigger"] = _json.dumps(
                {"qlHidden": {"n": n, "counts": _selection_payload(job)}})
        return resp
    return HTMLResponse("")


@app.post("/jobs/{job_id}/retry")
async def job_retry(request: Request, job_id: str):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    job = job_mgr.registry.get(job_id)
    if not job or job.status != job_mgr.JobStatus.FAILED or not job.album_id:
        return RedirectResponse(url="/queue", status_code=303)
    album_id = job.album_id
    duplicate = _find_job_touching_album(album_id)
    if duplicate:
        return RedirectResponse(url=f"/jobs/{duplicate.id}", status_code=303)
    try:
        token = _get_token()
        from qobuz_librarian.api.client import call_within
        from qobuz_librarian.api.search import get_album
        loop = asyncio.get_running_loop()
        album = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: call_within(cfg.WEB_FETCH_TIMEOUT, get_album, album_id, token)),
            timeout=cfg.WEB_FETCH_TIMEOUT,
        )
        title = album.get("title") or job.title or "?"
        artist = (album.get("artist") or {}).get("name") or job.artist or "?"
        # Re-check for a duplicate under the submit lock: the await above yielded
        # the event loop, so a second Retry for the same album could have raced
        # in between the pre-check and here. Same guard queue_download uses, so
        # two quick retries can't double-queue the same album.
        with _DOWNLOAD_SUBMIT_LOCK:
            duplicate = _find_job_touching_album(album_id)
            if duplicate:
                return RedirectResponse(url=f"/jobs/{duplicate.id}", status_code=303)
            # set_mode could have handed the lock to the terminal during the
            # get_album await above; re-check inside the submit lock (as
            # queue_download does) so a retry can't start a job after the CLI
            # handoff.
            busy = _lock_busy_response(request)
            if busy is not None:
                return busy
            # A failed single-track grab carries job.album_id (so Retry shows up),
            # but _make_download_run would re-grab the WHOLE album. Rebuild it as
            # the same one-track run instead.
            single = getattr(job, "single", None)
            track = None
            if single and single.get("track_id"):
                tid = str(single.get("track_id"))
                track = next(
                    (t for t in (album.get("tracks") or {}).get("items") or []
                     if str(t.get("id")) == tid), None)
            new_job = job_mgr.Job(title=title, artist=artist, album_id=album_id)
            if track is not None:
                new_job.single = dict(single)
                job_mgr.submit(new_job, _make_single_track_run(album, track, token))
            elif single and single.get("track_id"):
                # The original was a single-track grab but that track is no
                # longer on Qobuz — do NOT silently re-download the whole album.
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(
                        "That track is no longer on Qobuz — nothing to retry."),
                    status_code=303)
            else:
                job_mgr.submit(new_job, _make_download_run(album, token))
        return RedirectResponse(url=f"/jobs/{new_job.id}", status_code=303)
    except (SystemExit, NoCredsError):
        return RedirectResponse(url="/settings?error=creds", status_code=303)
    except Exception:
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote("Retry failed — check your token."),
            status_code=303,
        )


@app.post("/jobs/{job_id}/undo")
async def job_undo(request: Request, job_id: str):
    """Reverse a single-track grab: delete the track it added, drop the beets row
    for it, undo the single mark, and remove a folder the grab created if it's
    now empty. Available while the job is still in memory."""
    # Undo deletes files and touches the beets DB, so it needs the same run-lock
    # gate every other mutating route has — the in-process staging lock below
    # can't keep it off the library while a CLI session holds the cross-process
    # lock (handed-off mode, or another instance).
    busy = _lock_busy_response(request)
    if busy is not None:
        if _is_htmx(request):
            return HTMLResponse(
                f'<div id="job-content">{busy.body.decode()}</div>')
        return busy
    job = job_mgr.registry.get(job_id)
    info = dict(getattr(job, "single", None) or {}) if job else {}
    if not job or not info.get("dir") or info.get("removed"):
        if _is_htmx(request):
            if job:
                return _tr(request, "_job_body.html", {"job": job})
            return HTMLResponse("", headers={"HX-Redirect": "/queue"})
        return RedirectResponse(url="/queue", status_code=303)

    def _reverse():
        from pathlib import Path

        from qobuz_librarian.integrations.beets import forget_beets_entries
        from qobuz_librarian.library import hidden as hidden_mod
        from qobuz_librarian.library.scanner import read_album_dir
        d = Path(info["dir"])
        want = (info.get("isrc") or "").replace("-", "").upper().strip()
        removed = None
        try:
            for et in read_album_dir(d):
                ei = (et.get("isrc") or "").replace("-", "").upper().strip()
                # read_album_dir keys the track number as "tracknumber"; match on
                # that, and only fall back to it when there's no ISRC AND we have a
                # real number to compare — two missing numbers must never read as
                # equal, or the fallback would delete an unrelated track.
                same = ((want and ei == want)
                        or (not want and info.get("track_no") is not None
                            and et.get("tracknumber") == info.get("track_no")))
                if same:
                    p = Path(et.get("path") or "")
                    if p.exists():
                        p.unlink()
                        removed = p
                    break
        except OSError:
            pass
        if removed is not None:
            forget_beets_entries([removed])
            if info.get("marked"):
                hidden_mod.unmark_single(info.get("artist") or "", info.get("album") or "")
        # If the grab created a brand-new folder and it now holds no audio, take
        # it back out so a one-off sample doesn't leave an empty album dir behind.
        try:
            if (info.get("new_folder") and d.is_dir()
                    and not any(x.is_file()
                                and x.suffix.lower() in cfg.AUDIO_EXTS
                                for x in d.rglob("*"))):
                import shutil
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
        # Return the Path actually deleted (or None when nothing matched) so the
        # caller can tell a real removal from a no-match — `removed is not None`
        # would collapse to a bool and make the not-found branch dead code,
        # reporting false success and burning the one-shot.
        return removed

    def _reverse_under_lock():
        # Take the lock inside the worker thread, never on the event loop —
        # holding a threading.Lock on the loop would freeze every other request
        # while a download worker (which may rip for minutes) holds it.
        with job_mgr.staging_lock():
            return _reverse()

    loop = asyncio.get_running_loop()
    removed = await loop.run_in_executor(None, _reverse_under_lock)
    if removed is not None:
        job.single = {**info, "removed": True}
        job.summary = f"Removed “{info.get('title')}” and undid the single."
    else:
        # File not found at all (deleted externally) — still burn the one-shot
        # so Undo doesn't loop, but only when the dir is gone too. If the dir
        # exists but no track matched (ISRC/track_no mismatch), leave removed
        # unset so the user can attempt a manual fix and retry.
        from pathlib import Path as _Path
        dir_gone = not _Path(info["dir"]).exists()
        if dir_gone:
            job.single = {**info, "removed": True}
            job.summary = f"“{info.get('title')}” was already gone — cleared the single mark."
        else:
            job.summary = (f"Couldn't find “{info.get('title')}” by ISRC/track number "
                           "— check the folder manually and delete the file if needed.")
    if _is_htmx(request):
        return _tr(request, "_job_body.html", {"job": job})
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(request: Request, job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        return RedirectResponse(url="/queue", status_code=303)
    was_review = job.status == job_mgr.JobStatus.AWAITING_REVIEW
    # Offload: cancelling a parked review runs cancel_review -> persist (a
    # json.dumps of the full candidate list + SQLite commit), which would block
    # the event loop and stall every SSE stream for a large review.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: job_mgr.request_cancel(job))
    # A review discard is instant → queue. A running/scanning job stops
    # cooperatively → keep them on the job page to watch it wind down.
    dest = "/queue" if was_review else f"/jobs/{job_id}"
    return RedirectResponse(url=dest, status_code=303)


@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, error: str = ""):
    """The Queue tab: jobs in flight (pending / scanning / running / awaiting
    review). Finished jobs live in the History tab, which reads the durable
    archive rather than the capped in-memory set."""
    return _tr(request, "queue.html", {
        "pending": job_mgr.registry.pending_and_running(),
        "error": error[:200],
        "page": "queue",
        "active_tab": "queue",
    })


_HISTORY_PER_PAGE = 30


@app.get("/queue/history", response_class=HTMLResponse)
async def queue_history(request: Request, p: int = 1):
    """The History tab: every finished job, newest first, paged from jobs.db so
    the record outlives the in-memory cap (which only the Queue/SSE views use)."""
    from datetime import datetime

    from qobuz_librarian.web import job_persistence
    p = max(1, p)

    def _load_page(page):
        total = job_persistence.history_count()
        pages = max(1, (total + _HISTORY_PER_PAGE - 1) // _HISTORY_PER_PAGE)
        page = min(max(1, page), pages)
        rows = job_persistence.history_page(_HISTORY_PER_PAGE, (page - 1) * _HISTORY_PER_PAGE)
        for r in rows:
            ts = r.get("finished_at") or r.get("created_at")
            r["when"] = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
        return total, pages, page, rows

    loop = asyncio.get_running_loop()
    total, pages, p, rows = await loop.run_in_executor(None, lambda: _load_page(p))
    return _tr(request, "history.html", {
        "page": "queue", "active_tab": "history",
        "jobs": rows, "cur_page": p, "pages": pages, "total": total,
    })


@app.post("/queue/clear")
async def queue_clear():
    """Clear the History: drop finished/canceled/failed jobs from the registry
    and the full on-disk archive. In-flight jobs are untouched."""
    from qobuz_librarian.web import job_persistence
    job_mgr.registry.clear_finished()
    job_persistence.clear_history()
    return RedirectResponse(url="/queue/history", status_code=303)


@app.post("/queue/cancel-pending")
async def queue_cancel_pending():
    for j in list(job_mgr.registry.pending_and_running()):
        job_mgr.request_cancel(j)
    return RedirectResponse(url="/queue", status_code=303)


def _diagnostics():
    """Read-only health checks surfaced on the Settings page."""
    import os
    import shutil as _sh

    checks = []

    def _dir_check(label, path, *, want_writable):
        p = Path(path)
        if not p.exists():
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} does not exist (volume not mounted?)"})
            return
        if not p.is_dir():
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} exists but is not a directory"})
            return
        if want_writable and not os.access(p, os.W_OK):
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} is not writable by the container user — "
                           "on a NAS, set PUID/PGID in .env to your media-share owner"})
            return
        try:
            n = sum(1 for _ in p.iterdir())
        except OSError as e:
            checks.append({"label": label, "ok": False,
                           "detail": f"{p} unreadable: {e}"})
            return
        checks.append({"label": label, "ok": True,
                       "detail": f"{p} — {n} entr{'y' if n == 1 else 'ies'}"})

    _dir_check("Music library (MUSIC_ROOT)", cfg.MUSIC_ROOT, want_writable=True)
    _dir_check("Staging (STAGING_DIR)", cfg.STAGING_DIR, want_writable=True)

    beets_db = Path(cfg.BEETS_DB_PATH)
    if beets_db.exists():
        ok = os.access(beets_db, os.R_OK)
        checks.append({"label": "beets DB (BEETS_DB_PATH)", "ok": ok,
                       "detail": f"{beets_db}" if ok
                       else f"{beets_db} exists but is not readable"})
    elif beets_db.parent.exists():
        checks.append({"label": "beets DB (BEETS_DB_PATH)", "ok": True,
                       "detail": f"{beets_db} (created on first import)"})
    else:
        checks.append({"label": "beets DB (BEETS_DB_PATH)", "ok": False,
                       "detail": f"{beets_db.parent} does not exist"})

    for binary in ("rip", "beet", "ffmpeg", "flac"):
        found = _sh.which(binary)
        checks.append({"label": f"`{binary}` binary",
                       "ok": bool(found),
                       "detail": found or f"{binary} not on PATH — "
                       "rebuild the image (docker compose build)"})

    stranded = []
    if cfg.UPGRADE_BACKUP_DIR.exists():
        try:
            for entry in cfg.UPGRADE_BACKUP_DIR.iterdir():
                if entry.is_dir() and (entry.suffix == ".partial"
                                       or entry.name == ".restore_trash"):
                    stranded.append(entry)
        except OSError:
            pass
    if stranded:
        checks.append({"label": "Stranded upgrade backups", "ok": False,
                       "detail": f"{len(stranded)} found in "
                                 f"{cfg.UPGRADE_BACKUP_DIR} — manual cleanup needed"})
    else:
        checks.append({"label": "Stranded upgrade backups", "ok": True,
                       "detail": "none"})

    # Backups whose original is still missing the tracks they hold — orphaned by
    # a hard kill that skipped the restore/delete. Retention keeps these rather
    # than reaping the only copy; surface them so they can be reconciled.
    try:
        from qobuz_librarian.library.backup import find_only_copy_backups
        orphans = find_only_copy_backups()
    except Exception:
        orphans = []
    if orphans:
        first = orphans[0]
        hint = f" e.g. restore {first[0].name!r} → {first[1]}" if first[1] else ""
        checks.append({"label": "Orphaned backups (only copy)", "ok": False,
                       "detail": f"{len(orphans)} backup(s) hold tracks missing "
                                 f"from their album folder.{hint}"})
    else:
        checks.append({"label": "Orphaned backups (only copy)", "ok": True,
                       "detail": "none"})
    return checks


def _resolve_host_path(container_path: str) -> tuple[str, bool]:
    """Return (display_path, is_host_path) for a path inside the container.

    Walks /proc/self/mountinfo to find the longest-prefix bind mount, then
    appends the remaining suffix to the host source. Falls back to the
    container path when no bind mount covers it (anonymous volume) or the
    file isn't available (non-Linux).
    """
    container_path = str(container_path)
    try:
        with open("/proc/self/mountinfo") as f:
            entries = []
            for line in f:
                parts = line.split()
                if len(parts) < 5:
                    continue
                entries.append((parts[4], parts[3]))  # mount_point, host_root
    except OSError:
        return container_path, False
    best = None
    for mount_point, host_root in entries:
        if mount_point == "/":  # container rootfs, not a user bind mount
            continue
        if (container_path == mount_point
                or container_path.startswith(mount_point.rstrip("/") + "/")):
            if best is None or len(mount_point) > len(best[0]):
                best = (mount_point, host_root)
    if best is None:
        return container_path, False
    mount_point, host_root = best
    suffix = container_path[len(mount_point):]
    host_path = host_root.rstrip("/") + suffix if suffix else host_root
    return host_path, True


def _settings_response(request, *, saved=False, queued=False, connected=False,
                       unverified=False, error="", mode="", user_id=None,
                       auth_token_prefill="", diagnostics=None, warnings=None):
    from qobuz_librarian.web import settings_store
    creds = _read_creds()
    values = settings_store.current()
    # If credentials come from QOBUZ_USER_AUTH_TOKEN env, anything saved
    # via the form is overridden on next process start — let the user know.
    import os
    # cfg.QOBUZ_USER_AUTH_TOKEN resolves QOBUZ_USER_AUTH_TOKEN_FILE too (the
    # secret is no longer re-exported to os.environ), so a *_FILE deployment is
    # correctly recognised as env-provided.
    creds_from_env = bool(cfg.QOBUZ_USER_AUTH_TOKEN)
    cli_only_env = os.environ.get("QL_CLI_ONLY", "").strip().lower() in (
        "1", "true", "yes", "on")
    return _tr(request, "settings.html", {
        "user_id": creds.get("user_id", "") if user_id is None else user_id,
        "auth_token_set": bool(creds.get("auth_token")),
        "auth_token_prefill": auth_token_prefill,
        "creds_from_env": creds_from_env,
        "cli_only_env": cli_only_env,
        "mode_changed": (mode or "").strip().lower(),
        "saved": saved,
        "queued": queued,
        "connected": connected,
        "unverified": unverified,
        "error": error,
        "warnings": warnings or [],
        "page": "settings",
        "library_paths": [
            {"label": label, "container": cp,
             "host": host, "resolved": resolved}
            for label, cp in (
                ("Music library", cfg.MUSIC_ROOT),
                ("Staging area", cfg.STAGING_DIR),
                ("Beets database", cfg.BEETS_DB_PATH),
                ("Streamrip config", cfg.STREAMRIP_CONFIG),
            )
            for host, resolved in [_resolve_host_path(cp)]
        ],
        "behavior_fields": settings_store.BEHAVIOR_FIELDS,
        "text_fields": settings_store.TEXT_FIELDS,
        "option_labels": settings_store.ENUM_OPTION_LABELS,
        "behavior": values,
        "diagnostics": diagnostics if diagnostics is not None else _diagnostics(),
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: bool = False,
                        queued: bool = False, connected: bool = False,
                        unverified: bool = False, error: str = "",
                        mode: str = ""):
    loop = asyncio.get_running_loop()
    diags = await loop.run_in_executor(None, _diagnostics)
    return _settings_response(request, saved=saved, queued=queued,
                              connected=connected, unverified=unverified,
                              error=error, mode=mode, diagnostics=diags)


def _streamrip_has_userid() -> bool:
    """True if the streamrip config carries a non-empty user id, so `rip` can
    actually authenticate a download. A token-only env (QOBUZ_USER_AUTH_TOKEN set,
    QOBUZ_USER_ID unset) has none until the id is set or creds are saved, even
    though the app's own Qobuz API calls work from the token alone."""
    try:
        if not cfg.STREAMRIP_CONFIG.exists():
            return False
        import tomllib
        with open(cfg.STREAMRIP_CONFIG, "rb") as f:
            data = tomllib.load(f)
        uid = str(data.get("qobuz", {}).get("email_or_userid", "") or "").strip()
        return bool(uid)
    except Exception:
        return False


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request, user_id: str = Form(""), auth_token: str = Form("")):
    global _TOKEN_VALID
    loop = asyncio.get_running_loop()
    diags = await loop.run_in_executor(None, _diagnostics)
    existing = _read_creds()
    # First-run with empty inputs: nothing to save and no creds to keep —
    # bounce back with a banner rather than writing blanks and flashing green.
    if not auth_token.strip() and not user_id.strip() \
            and not existing.get("auth_token") \
            and not cfg.QOBUZ_USER_AUTH_TOKEN:
        return RedirectResponse(url="/settings?error=empty", status_code=303)
    # Blank means "keep the existing value" — the fields are not pre-filled,
    # so an empty submission must not wipe a previously-saved credential.
    if not auth_token.strip() and not user_id.strip() and cfg.QOBUZ_USER_AUTH_TOKEN:
        # Blank submit with an env token = "keep the env creds". But downloads
        # shell out to `rip`, which also needs a user id; a token-only env
        # authenticates our own API calls yet fails every download. Only report
        # connected when a usable user id actually exists (env id, or one already
        # in the rip config) — otherwise show the needuser banner instead of a
        # false green that dead-ends at the first download.
        if cfg.QOBUZ_USER_ID or _streamrip_has_userid():
            return RedirectResponse(url="/settings?connected=1", status_code=303)
        return _settings_response(request, error="needuser", user_id="",
                                  auth_token_prefill="", diagnostics=diags)
    new_token = auth_token.strip() or existing.get("auth_token", "")
    new_uid = user_id.strip() or existing.get("user_id", "")
    # Both fields are mandatory. The token authenticates our API calls on its
    # own, so the check below passes with just a token — but load_qobuz_token()
    # and streamrip's login() both require the user id, so a token-only save
    # would look connected yet fail with "no credentials" on the first search,
    # and downloads would raise MissingCredentialsError. Refuse a half-config
    # and name the missing field. Re-render rather than redirect on these two:
    # the user typed something that didn't save, and a fresh GET can't pre-fill
    # the password field — so a redirect would wipe the (long) token they just
    # pasted.
    if new_token and not new_uid:
        return _settings_response(request, error="needuser",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    if new_uid and not new_token:
        return _settings_response(request, error="empty",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    # Check the token with Qobuz *before* writing it. A token Qobuz outright
    # rejects never lands in the config — we re-render with it still in the box
    # so the user can fix a paste slip without losing it. A network/timeout
    # failure can't tell us either way, so we save and flag it unverified.
    verdict = "unreachable"
    if new_token:
        from qobuz_librarian.api.client import call_within
        try:
            verdict = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: call_within(cfg.WEB_TEST_AUTH_TIMEOUT,
                                              _classify_token, new_token)),
                timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            verdict = "unreachable"
    if verdict == "rejected":
        # Re-render with the real token still in the (password-type, so
        # visually masked) field so the user can fix a paste slip without
        # re-typing it — same as the needuser/empty/creds branches.
        return _settings_response(request, error="rejected",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    ok = _write_creds(new_uid, new_token)
    if not ok:
        return _settings_response(request, error="creds",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=auth_token.strip(),
                                  diagnostics=diags)
    # Keep the dashboard's "token isn't authenticating" banner in step with
    # what we just verified — a freshly-fixed token shouldn't keep nagging
    # until the next restart. An unverified save drops back to inconclusive
    # rather than leaving a stale False from an earlier probe.
    _TOKEN_VALID = True if verdict == "ok" else None
    suffix = "&unverified=1" if verdict == "unreachable" else ""
    return RedirectResponse(url=f"/settings?connected=1{suffix}", status_code=303)


@app.post("/settings/behavior", response_class=HTMLResponse)
async def save_behavior(request: Request):
    from qobuz_librarian.web import settings_store
    form = await request.form()
    # The real Settings form ships a hidden form_complete=1 marker. When
    # it's present, every checkbox key is known to be authoritative
    # (unchecked = absent = False). When it's absent — a scripted partial
    # POST — only the keys the caller actually sent overwrite; the rest
    # are left at their current value so a one-field toggle doesn't blow
    # away the user's other booleans.
    is_complete = "form_complete" in form
    if is_complete:
        values = {k: (k in form) for k in settings_store.BEHAVIOR_KEYS}
    else:
        values = {k: (form.get(k, "").strip().lower() not in ("0", "false", "off", "no", ""))
                  for k in settings_store.BEHAVIOR_KEYS if k in form}
    # Text/enum/list fields: take whatever the form posted; absent =
    # leave unchanged (don't wipe a previously-set value).
    for k in settings_store.TEXT_KEYS:
        if k in form:
            values[k] = form.get(k, "")
    ok, warnings = settings_store.save(values)
    # Applied in-memory regardless; error only means it won't persist.
    if not ok:
        return RedirectResponse(url="/settings?error=persist", status_code=303)
    if warnings:
        # Re-render in place so we can name exactly which entries were dropped
        # (a misspelt provider, an uninstalled beets plugin) without smuggling
        # user-typed values through the redirect URL.
        loop = asyncio.get_running_loop()
        diags = await loop.run_in_executor(None, _diagnostics)
        return _settings_response(request, saved=True,
                                  queued=settings_store._any_active_job(),
                                  warnings=warnings, diagnostics=diags)
    suffix = "&queued=1" if settings_store._any_active_job() else ""
    return RedirectResponse(url=f"/settings?saved=1{suffix}", status_code=303)


@app.post("/settings/mode")
async def set_mode(request: Request, target: str = Form("")):
    """Hand the run-lock to the terminal (CLI), or take it back for the web.

    Switching to CLI is refused while a download/scan is active — releasing the
    lock under a running job would let the CLI race the worker over /staging.
    """
    global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _CLI_MODE, _creds_cache
    from qobuz_librarian import run_lock
    want = (target or "").strip().lower()
    if want == "cli":
        # Flip to CLI mode first so a /download or scan POST landing during the
        # handoff is refused (503) instead of slipping past the check and racing
        # the CLI over /staging once we release the lock below.
        _CLI_MODE = True
        if job_mgr.registry.pending_and_running():
            _CLI_MODE = False  # nothing handed off — stay in web mode
            return RedirectResponse(url="/settings?error=" + urllib.parse.quote(
                "Finish or cancel the active download before handing off to the "
                "terminal."), status_code=303)
        if _RUN_LOCK_HANDLE is not None:
            try:
                _RUN_LOCK_HANDLE.close()  # closing the handle releases the flock
            except OSError:
                pass
            _RUN_LOCK_HANDLE = None
        _LOCK_BUSY_PID = None
        return RedirectResponse(url="/settings?mode=cli", status_code=303)
    if want == "web":
        try:
            _RUN_LOCK_HANDLE = run_lock.acquire()
            _CLI_MODE = False
            _LOCK_BUSY_PID = None
            # The CLI may have changed the saved token while it held the lock;
            # drop the cached creds so the banner reflects what's on disk now.
            _creds_cache = None
            return RedirectResponse(url="/settings?mode=web", status_code=303)
        except run_lock.LockBusy:
            # A CLI session still holds the lock — can't take it back yet.
            return RedirectResponse(url="/settings?error=" + urllib.parse.quote(
                "The terminal is still using it — finish your CLI command, then "
                "resume."), status_code=303)
    return RedirectResponse(url="/settings", status_code=303)


# Empty 500ms ticks before we emit a `: ping` heartbeat to keep
# reverse proxies from dropping the EventSource on a quiet scan.
# Defaults from cfg.SSE_HEARTBEAT_TICKS / cfg.SSE_MAX_WORKERS (env-tunable).
_SSE_HEARTBEAT_TICKS = cfg.SSE_HEARTBEAT_TICKS

# Dedicated thread pool for SSE waits so a long-running scan with many
# tabs open doesn't starve /search and /download on the default executor.
_SSE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=cfg.SSE_MAX_WORKERS, thread_name_prefix="sse")


@app.get("/api/diagnostics", response_class=HTMLResponse)
async def api_diagnostics(request: Request):
    """Htmx partial — returns just the diagnostics list items for the Recheck button."""
    loop = asyncio.get_running_loop()
    checks = await loop.run_in_executor(None, _diagnostics)
    rows = []
    for d in checks:
        icon = "✓" if d["ok"] else "✗"
        cls = "badge-success" if d["ok"] else "badge-error"
        detail = f'<div class="font-mono text-xs text-base-content/60 break-all">{html.escape(d.get("detail") or "")}</div>' if d.get("detail") else ""
        rows.append(
            f'<div class="flex items-start gap-3">'
            f'<span class="shrink-0 mt-0.5"><span class="badge badge-sm {cls}">{icon}</span></span>'
            f'<div class="min-w-0"><div class="text-sm">{html.escape(d["label"])}</div>{detail}</div>'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows))


@app.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def _generator():
        import logging as _logging
        import queue as _queue
        # Reconnect quickly so a backgrounded tab's progress bar catches up to
        # the live count soon after it's brought back to the foreground.
        yield "retry: 750\n\n"
        if (job.status in job_mgr.TERMINAL
                or job.status == job_mgr.JobStatus.AWAITING_REVIEW):
            for line in job.log_lines[-job.REPLAY_TAIL:]:
                escaped = line.replace("\n", " ").replace("\r", "")
                yield f"data: {escaped}\n\n"
            yield f"event: done\ndata: {job.status.value}\n\n"
            return
        sub = job.subscribe()
        loop = asyncio.get_running_loop()
        empty_ticks = 0
        try:
            while True:
                try:
                    line = await loop.run_in_executor(
                        _SSE_EXECUTOR, lambda: sub.get(timeout=0.5))
                    empty_ticks = 0
                    if line == job_mgr.STREAM_END:
                        yield f"event: done\ndata: {job.status.value}\n\n"
                        break
                    if line.startswith(job_mgr.PROGRESS_PREFIX):
                        yield ("event: progress\ndata: "
                               + line[len(job_mgr.PROGRESS_PREFIX):] + "\n\n")
                        continue
                    if line == job_mgr.REVIEW_CHANGED:
                        continue  # review-sync nudge — handled by the review stream
                    escaped = line.replace("\n", " ").replace("\r", "")
                    yield f"data: {escaped}\n\n"
                except _queue.Empty:
                    if (job.status in job_mgr.TERMINAL
                            or job.status == job_mgr.JobStatus.AWAITING_REVIEW):
                        yield f"event: done\ndata: {job.status.value}\n\n"
                        break
                    empty_ticks += 1
                    if empty_ticks >= _SSE_HEARTBEAT_TICKS:
                        empty_ticks = 0
                        yield ": ping\n\n"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logging.getLogger("qobuz_librarian").exception(
                        "SSE stream error for job %s", job.id)
                    break
        finally:
            job.unsubscribe(sub)

    return StreamingResponse(_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{job_id}/review-stream")
async def job_review_stream(job_id: str):
    """Live channel for an awaiting-review page: emits `event: review` whenever
    selection or candidates change (a tick/untick/hide in this or another tab),
    so every open view stays in sync. Closes once the job leaves review (the
    page then reloads to show the executing/finished state). Separate from the
    progress stream, which closes the moment a scan finishes."""
    # Only a LIVE job (in the registry) has a producer that fans out review
    # nudges; a historical/evicted review still renders and saves selection via
    # the disk fallback, but can't receive live cross-tab updates — so end its
    # stream cleanly rather than 404 (which surfaces as a console error) or hold
    # a socket that never gets a nudge.
    job = job_mgr.registry.get(job_id)

    async def _generator():
        import queue as _queue
        yield "retry: 1000\n\n"
        if job is None or job.status != job_mgr.JobStatus.AWAITING_REVIEW:
            yield "event: closed\ndata: inactive\n\n"
            return
        sub = job.subscribe()
        loop = asyncio.get_running_loop()
        empty_ticks = 0
        try:
            while True:
                try:
                    line = await loop.run_in_executor(
                        _SSE_EXECUTOR, lambda: sub.get(timeout=0.5))
                    if line == job_mgr.REVIEW_CHANGED:
                        yield "event: review\ndata: changed\n\n"
                    # All other fanned-out lines (log/progress/end) are ignored
                    # here — this channel only carries review-sync nudges.
                except _queue.Empty:
                    if job.status != job_mgr.JobStatus.AWAITING_REVIEW:
                        yield f"event: closed\ndata: {job.status.value}\n\n"
                        break
                    empty_ticks += 1
                    if empty_ticks >= _SSE_HEARTBEAT_TICKS:
                        empty_ticks = 0
                        yield ": ping\n\n"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    break
        finally:
            job.unsubscribe(sub)

    return StreamingResponse(_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _job_to_dict(job, *, log_tail: int = 50):
    out = {
        "id": job.id,
        "status": job.status.value,
        "title": job.title,
        "artist": job.artist,
        "album_id": getattr(job, "album_id", None),
        "error": job.error,
        "created_at": getattr(job, "created_at", None),
        "finished_at": getattr(job, "finished_at", None),
    }
    if log_tail:
        out["log_lines"] = job.log_lines[-log_tail:]
    return out


@app.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_dict(job)


@app.get("/api/jobs")
async def jobs_list(status: str = "", limit: int = 50):
    """List jobs as JSON. Optional `status` filter ('pending', 'running',
    'awaiting_review', 'scanning', 'done', 'failed', 'canceled').
    `limit` caps the response — most recent first."""
    wanted = status.strip().lower() or None
    if wanted is not None:
        valid = {s.value for s in job_mgr.JobStatus}
        if wanted not in valid:
            raise HTTPException(status_code=400,
                                detail="Unknown status filter")
    cap = max(1, min(limit, 500))
    matching = []
    for j in reversed(job_mgr.registry.all()):
        if wanted and j.status.value != wanted:
            continue
        matching.append(_job_to_dict(j, log_tail=0))
        if len(matching) >= cap:
            break
    return JSONResponse({"jobs": matching, "count": len(matching)})


def _get_token():
    from qobuz_librarian.api.auth import load_qobuz_token
    return load_qobuz_token()[1]


def _format_age(ts: float) -> str:
    """Human-readable age of a past timestamp."""
    import time as _time
    age = _time.time() - ts
    if age < 120:
        return "just now"
    if age < 3600:
        return f"{int(age / 60)} min ago"
    if age < 86400:
        return f"{int(age / 3600)} hr ago"
    days = int(age / 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def _last_scan_age() -> str | None:
    """Human-readable age of the last library/artist scan, or None."""
    try:
        ts = float(cfg.LAST_SCAN_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return _format_age(ts)


def _last_new_release_check_age() -> str | None:
    """Human-readable age of the last new-release check, or None — gives the
    dashboard a sibling indicator to the existing 'last library scan' line so
    the user can see how fresh the auto-check's signal is."""
    from qobuz_librarian.library import new_releases
    ts = new_releases.last_run()
    return _format_age(ts) if ts is not None else None


def _no_creds_response(request):
    """Return a 303 redirect (or htmx fragment) when no credentials are set."""
    if _is_htmx(request):
        return HTMLResponse(
            '<div class="alert alert-error" data-flash>No Qobuz credentials set — '
            'visit <a href="/settings" class="link">Settings</a>.</div>',
            status_code=200)
    return RedirectResponse(url="/settings?error=creds", status_code=303)


_creds_cache: dict | None = None


def _read_creds():
    global _creds_cache
    # cfg resolves QOBUZ_USER_AUTH_TOKEN_FILE too (the secret is no longer
    # re-exported to os.environ), so a *_FILE deployment is recognised here.
    env_token = cfg.QOBUZ_USER_AUTH_TOKEN
    if env_token:
        return {"user_id": cfg.QOBUZ_USER_ID or "", "auth_token": env_token}
    if _creds_cache is not None:
        return _creds_cache
    if not cfg.STREAMRIP_CONFIG.exists():
        return {}
    try:
        import tomllib
        with open(cfg.STREAMRIP_CONFIG, "rb") as f:
            data = tomllib.load(f)
        qz = data.get("qobuz", {})
        _creds_cache = {"user_id": qz.get("email_or_userid", ""),
                        "auth_token": qz.get("password_or_token", "")}
        return _creds_cache
    except Exception:
        return {}


def _write_creds(user_id, auth_token) -> bool:
    """Write credentials into the streamrip config. Returns False if the
    config volume isn't writable (NAS perms) so the Settings page can show
    a clear message rather than 500ing.

    Delegates to qobuz_librarian.api.auth.write_streamrip_creds so the web
    Settings path and the env-var sync share one credential writer."""
    global _creds_cache
    _creds_cache = None
    from qobuz_librarian.api.auth import write_streamrip_creds
    return write_streamrip_creds(user_id, auth_token)


def start():
    import uvicorn
    uvicorn.run("qobuz_librarian.web.app:app", host=cfg.WEB_HOST, port=cfg.WEB_PORT, workers=1)
