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
    FileResponse,
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
            f'<div class="alert alert-error">{html.escape(msg)}</div>',
            status_code=200)
    return templates.TemplateResponse(
        request=request, name="lock_busy.html",
        context={"msg": msg}, status_code=503)


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
    return lambda j, chosen: flows.execute_migration(
        j, chosen, dest, in_place=in_place)


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
        if cred_status == "applied":
            _log.info("Configured the web login from WEB_AUTH_USER / "
                      "WEB_AUTH_PASSWORD.")
        elif cred_status == "partial":
            _log.warning("Set both WEB_AUTH_USER and WEB_AUTH_PASSWORD to seed "
                         "the web login from the environment — only one was set.")
        elif cred_status == "failed":
            _log.warning("Couldn't write the web login from the environment; "
                         "the data volume may not be writable.")
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
        for label, path in (("/staging", cfg.STAGING_DIR),
                            ("/music", cfg.MUSIC_ROOT)):
            if not Path(path).exists() or not os.access(str(path), os.W_OK):
                _UNWRITABLE_VOLUMES.append(label)
        if _UNWRITABLE_VOLUMES:
            _log.error("STARTUP: critical volumes not writable: %s. Write "
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
    try:
        from qobuz_librarian.library import flac_cache
        n_pruned = flac_cache.prune_missing()
        if n_pruned:
            _log.info("Pruned %d stale tag-cache entries at startup.", n_pruned)
    except Exception as e:
        _log.debug("flac-cache prune error at startup: %s", e)
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
    # Done before start_worker() so the worker doesn't race with restore.
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
              lifespan=_lifespan)

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
    _APP_VERSION = "0.6.0"
templates.env.globals["app_version"] = _APP_VERSION
templates.env.globals["repo_url"] = "https://github.com/jarynclouatre/qobuz-librarian"
# Whether to show a Log out control — true only when auth is on and set up.
templates.env.globals["auth_active"] = web_auth.auth_active

static_dir = _here / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        str(static_dir / "sw.js"),
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
    if not web_auth.check_login_rate_limit(ip):
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Too many failed attempts — wait an hour and try again."},
            status_code=429)
    if not web_auth.verify_login(username.strip(), password):
        web_auth.record_login_failure(ip)
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Incorrect username or password."},
            status_code=401)
    web_auth.clear_login_failures(ip)
    resp = RedirectResponse(url="/", status_code=303)
    web_auth.set_session_cookie(resp, request)
    return resp


@app.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse(url="/login", status_code=303)
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
    context; partial-fragment renders skip this entirely.
    """
    if "pending_job_count" not in context or "queue_has_running" not in context:
        active = job_mgr.registry.pending_and_running()
        context.setdefault("pending_job_count", len(active))
        context.setdefault(
            "queue_has_running",
            any(j.status.value in ('running', 'scanning') for j in active),
        )
    context.setdefault("cli_mode", _CLI_MODE)
    return templates.TemplateResponse(request=request, name=name,
                                      context=context, status_code=status_code)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Render a styled page for a mistyped/stale URL instead of a bare
    ``{"detail": "Not Found"}``. API routes and every non-404 status keep the
    JSON shape callers expect."""
    if exc.status_code == 404 and not request.url.path.startswith("/api/"):
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


def _active_job():
    return job_mgr.registry.running_job()


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


def _active_library_scan():
    """A library scan that's already pending/crawling, or None."""
    for j in job_mgr.registry.pending_and_running():
        if (getattr(j, "execute_kind", "") == "library"
                and j.status.value in ("pending", "scanning")):
            return j
    return None


def _start_library_scan(partial_only=False):
    """Submit a library scan and return the job. Shared by the Library page and
    the automatic first-run/resume trigger. scan_library resumes from a matching
    checkpoint on its own, so this is the same call whether starting or resuming.

    Deduped under the lock: if a library scan is already crawling, return it
    instead of stacking a second one (the manual button and the auto trigger can
    both land here at once)."""
    with _auto_check_lock:
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
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Lyric retry")
    job_mgr.submit(job, lambda j: flows.run_lyric_retry(j, _get_token()))
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = Query("", max_length=500)):
    if q.strip():
        return await do_search(request, q=q)
    creds_ok = bool(_read_creds().get("auth_token"))
    return _tr(request, "search.html", {
        "q": "", "results": [], "error": None,
        "creds_ok": creds_ok, "page": "search",
    })


@app.post("/search", response_class=HTMLResponse)
async def do_search(request: Request, q: str = Form("", max_length=500)):
    results = []
    error = None
    query = q.strip()
    if query:
        # Imported before the try so the except clauses below can always name
        # them, even if a failure happens before the request reaches the API.
        from qobuz_librarian.api.auth import AuthLost, QobuzError
        try:
            token = _get_token()
            from qobuz_librarian.api.search import get_album, search_albums
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
            if parsed and parsed[0] == "album":
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
                except AuthLost:
                    raise
                except QobuzError:
                    error = "Couldn't fetch that album — check the URL."
                except Exception:
                    import logging
                    logging.getLogger("qobuz_librarian").exception(
                        "album fetch failed for %r", query)
                    error = "Couldn't fetch that album — check the URL."
            elif parsed and parsed[0] == "track":
                # A track URL text-searched would silently return nothing.
                # Mirror the CLI's diagnostic instead of a blank page.
                error = ("That's a track URL — Qobuz Librarian works on "
                         "albums. Paste the album URL instead.")
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
                try:
                    raw = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: call_within(cfg.WEB_FETCH_TIMEOUT, search_albums,
                                                query, token, limit=cfg.SEARCH_LIMIT),
                        ),
                        timeout=cfg.WEB_FETCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    error = "Timed out reaching the Qobuz API."

            for a in raw:
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
        except QobuzError:
            error = "Search failed — try again."
        except Exception:
            import logging
            logging.getLogger("qobuz_librarian").exception(
                "search failed for %r", query)
            error = "Search failed — try again."
    creds_ok = bool(_read_creds().get("auth_token"))
    ctx = {"q": query, "results": results, "error": error,
           "creds_ok": creds_ok, "page": "search"}
    if _is_htmx(request):
        return _tr(request, "_search_results.html", ctx)
    return _tr(request, "search.html", ctx)


def _make_download_run(album, token):
    """Return the run(j) callable used by both queue_download and job_retry."""
    def run(j):
        from qobuz_librarian.modes.process import process_album
        from qobuz_librarian.ui_cli.errors import plural
        from qobuz_librarian.web.flows import build_args
        with job_mgr.staging_lock():
            r = process_album(album, build_args(), allow_force=False,
                              already_confirmed=True, token=token) or {}
        benign = {"already_complete", "skipped_already_higher_quality",
                  "dry_run", "user_skipped", "lossy_only", "no_tracks",
                  "cancelled"}
        if r.get("result") not in benign and not r.get("imported"):
            j.status = job_mgr.JobStatus.FAILED
            j.error = (f"{plural(r['n_fail'], 'track')} failed"
                       if r.get("n_fail") else "download or import failed")
        elif r.get("imported") and r.get("n_fail", 0) > 0:
            j.error = f"{plural(r['n_fail'], 'track')} failed — see job log"
    return run


@app.post("/download", response_class=HTMLResponse)
async def queue_download(request: Request, album_id: str = Form(""),
                         force: str = Form("")):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    album_id = album_id.strip()
    if not album_id:
        msg = "Missing album id."
        if _is_htmx(request):
            return HTMLResponse(
                f'<div class="alert alert-error" data-flash>{msg}</div>',
                status_code=400)
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
    force_redownload = str(force).strip().lower() in ("1", "true", "yes", "on")
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
        if not force_redownload:
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
                msg = "You already own this album."
                if _is_htmx(request):
                    return HTMLResponse(
                        f'<div class="alert alert-warning" data-flash>{html.escape(msg)} '
                        f'<a href="/" class="link">Back to dashboard</a>.</div>')
                return RedirectResponse(
                    url="/queue?error=" + urllib.parse.quote(msg),
                    status_code=303)
        title  = album.get("title") or "?"
        artist = (album.get("artist") or {}).get("name") or "?"
        job = job_mgr.Job(title=title, artist=artist, album_id=album_id)

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
            job_mgr.submit(job, _make_download_run(album, token))
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


@app.post("/artist")
async def artist_scan(request: Request, artist: str = Form("")):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    name = artist.strip()[:200]
    if not name:
        return RedirectResponse(
            url="/artist?error=" + urllib.parse.quote("Artist name is required."),
            status_code=303,
        )
    if any(c in name for c in ("<", ">", "\x00")):
        return RedirectResponse(
            url="/artist?error=" + urllib.parse.quote(
                "Artist name contains forbidden characters."),
            status_code=303,
        )
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Artist scan", artist=name)
    job.execute_kind = "album"
    job_mgr.submit_scan(
        job,
        lambda j: flows.scan_artist(j, name, _get_token()),
        lambda j, chosen: flows.execute_albums(j, chosen, _get_token()),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request):
    from qobuz_librarian.library import hidden as hidden_mod
    creds_ok = bool(_read_creds().get("auth_token"))
    return _tr(request, "library.html", {
        "creds_ok": creds_ok, "page": "library",
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
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    # "library" (not "album") so the review screen knows this is the paced triage
    # surface; both modes run the same album executor and resume from a matching
    # checkpoint if one's waiting (see _start_library_scan / scan_library).
    job = await loop.run_in_executor(
        None, lambda: _start_library_scan(partial_only=(mode_norm == "partial_fill")))
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


def _hidden_view(request, scope, *, page, restore_action, back_url):
    from qobuz_librarian.library import hidden as hidden_mod
    return _tr(request, "hidden.html", {
        "page": page, "scope": scope, "back_url": back_url,
        "restore_action": restore_action,
        "groups": hidden_mod.hidden_by_artist(scope)})


async def _restore_hidden(request, scope, redirect):
    from qobuz_librarian.library import hidden as hidden_mod
    form = await request.form()
    artists = form.getlist("artist")[:10000]
    if artists:
        hidden_mod.restore(scope, artists)
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
    job_mgr.submit_scan(
        job,
        lambda j: flows.scan_upgrades(j, _get_token()),
        lambda j, chosen: flows.execute_upgrades(j, chosen, _get_token()),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/downsample", response_class=HTMLResponse)
async def downsample_page(request: Request):
    from qobuz_librarian.integrations.downsample_engine import HAVE_DOWNSAMPLE
    from qobuz_librarian.library import hidden as hidden_mod
    return _tr(request, "downsample.html", {
        "page": "downsample",
        "have_downsample": HAVE_DOWNSAMPLE,
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
    job_mgr.submit_scan(
        job,
        lambda j: flows.scan_downsamples(j),
        lambda j, chosen: flows.execute_downsamples(j, chosen),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/repair", response_class=HTMLResponse)
async def repair_page(request: Request):
    creds_ok = bool(_read_creds().get("auth_token"))
    return _tr(request, "repair.html", {"creds_ok": creds_ok, "page": "repair"})


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
    job_mgr.submit_scan(
        job,
        lambda j: flows.scan_repairs(j, _get_token()),
        lambda j, chosen: flows.execute_repairs(j, chosen, _get_token()),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/lyrics", response_class=HTMLResponse)
async def lyrics_page(request: Request):
    from qobuz_librarian.integrations.lyric_fetch import AVAILABLE
    providers = ", ".join(cfg.LYRICS_PROVIDERS) or "Lrclib, NetEase, Musixmatch"
    return _tr(request, "lyrics.html", {
        "page": "lyrics",
        "have_lyrics": AVAILABLE,
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
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Lyrics backfill")
    job_mgr.submit(
        job,
        lambda j: flows.run_library_lyrics(j, rescan=rescan, synced_only=synced_only),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/migrate", response_class=HTMLResponse)
async def migrate_page(request: Request):
    import os
    src, dest = cfg.MIGRATE_SRC, cfg.MIGRATE_DEST
    migrate_checks = []
    for label, path in (("Source folder", src), ("Destination folder", dest)):
        if not path:
            migrate_checks.append({"label": label, "ok": False, "detail": "not set"})
        else:
            p = Path(path)
            if not p.exists():
                migrate_checks.append({"label": label, "ok": False, "detail": f"{p} does not exist"})
            elif not p.is_dir():
                migrate_checks.append({"label": label, "ok": False, "detail": f"{p} is not a directory"})
            elif not os.access(str(p), os.R_OK):
                migrate_checks.append({"label": label, "ok": False, "detail": f"{p} is not readable"})
            else:
                migrate_checks.append({"label": label, "ok": True, "detail": str(p)})
    return _tr(request, "migrate.html", {
        "page": "migrate",
        "src": src,
        "dest": dest,
        "configured": bool(src and dest),
        "migrate_checks": migrate_checks,
    })


@app.post("/migrate")
async def migrate_scan(request: Request):
    # No credential check: migration only reads and reorganizes local files.
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    from qobuz_librarian.library import migrate as engine
    src, dest = cfg.MIGRATE_SRC, cfg.MIGRATE_DEST
    if not src or not dest:
        err = ("Set QL_MIGRATE_SRC and QL_MIGRATE_DEST — the folder to read and "
               "the folder to build the organized copy into — then try again.")
    else:
        err = engine.validate_paths(Path(src), Path(dest))
    if err:
        return _tr(request, "migrate.html", {
            "page": "migrate", "src": src, "dest": dest,
            "configured": bool(src and dest), "error": err})
    form = await request.form()
    use_acoustid = form.get("acoustid") == "on"
    in_place = form.get("in_place") == "on"
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Library migration")
    job.review_verb = "Move" if in_place else "Copy"
    job.execute_kind = "migration"
    job.execute_args = {"dest": str(dest), "in_place": bool(in_place)}
    job_mgr.submit_scan(
        job,
        lambda j: flows.scan_migration(j, src, dest, use_acoustid=use_acoustid,
                                       in_place=in_place),
        lambda j, chosen: flows.execute_migration(j, chosen, dest,
                                                  in_place=in_place, src=src),
    )
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
                   stale: bool = False):
    job = job_mgr.registry.get(job_id)
    historical = False
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return RedirectResponse(url="/queue", status_code=303)
        historical = True
    return _tr(request, "job.html", {"job": job, "page": "queue",
                                     "approved": approved, "stale": stale,
                                     "historical": historical,
                                     "JobStatus": job_mgr.JobStatus})


@app.get("/jobs/{job_id}/content", response_class=HTMLResponse)
async def job_content(request: Request, job_id: str):
    """The job page's state-specific body, on its own. The live page swaps
    this in when the SSE stream reports the job finished, so the terminal
    view has one render path — the server's — instead of a faked-up bar."""
    job = job_mgr.registry.get(job_id)
    if not job:
        job = job_mgr.load_historical_job(job_id)
        if job is None:
            return HTMLResponse("", status_code=404)
    return _tr(request, "_job_body.html", {"job": job,
                                           "JobStatus": job_mgr.JobStatus})


@app.post("/jobs/{job_id}/approve")
async def job_approve(request: Request, job_id: str):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    job = job_mgr.registry.get(job_id)
    if not job:
        return RedirectResponse(url="/queue", status_code=303)
    form = await request.form()
    # Cap at 10k cids — defensive against a forged form with megabytes of ids.
    selected = form.getlist("cid")[:10000]
    approved = job_mgr.approve(job, selected)
    flag = "approved=1" if approved else "stale=1"
    return RedirectResponse(url=f"/jobs/{job_id}?{flag}", status_code=303)


# Scan kinds that get the paced-triage surface (unticked, hideable, live-fill).
# Both share one review screen; they differ only in which hidden-store scope a
# dismiss writes — missing-album for the gap scan, upgrade for the upgrade scan.
_TRIAGE_KINDS = ("library", "upgrade", "new_releases", "downsample")


def _hide_scope(execute_kind):
    from qobuz_librarian.library import hidden as hidden_mod
    if execute_kind == "upgrade":
        return hidden_mod.SCOPE_UPGRADE
    if execute_kind == "downsample":
        return hidden_mod.SCOPE_DOWNSAMPLE
    return hidden_mod.SCOPE_MISSING


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
    job = job_mgr.registry.get(job_id)
    if not job:
        return HTMLResponse("", status_code=404)
    if (job.execute_kind in _TRIAGE_KINDS and job.status in (
            job_mgr.JobStatus.AWAITING_REVIEW, job_mgr.JobStatus.SCANNING)):
        from qobuz_librarian.web import flows
        form = await request.form()
        artist = (form.get("artist") or "").strip()
        keep = form.getlist("cid")[:10000]
        n = flows.dismiss_albums(job, artist, keep,
                                 scope=_hide_scope(job.execute_kind))
        remaining = [c for c in job.candidates if c.get("artist") == artist]
        if remaining:
            resp = _tr(request, "_review_group.html",
                       {"job": job, "artist": artist, "items": remaining,
                        "triage": True, "open": True})
        else:
            resp = HTMLResponse("")  # whole artist hidden — outerHTML drops it
        if n:
            resp.headers["HX-Trigger"] = '{"qlHidden": %d}' % n
        return resp
    return HTMLResponse("")


@app.get("/jobs/{job_id}/groups", response_class=HTMLResponse)
async def job_groups(request: Request, job_id: str, after: int = -1):
    """Render artist groups whose albums are newer than ``after`` (candidate
    seq). The live scan page polls this to append artists as the walk finds
    them, leaving the groups already on screen — and their tick/expand state —
    untouched."""
    job = job_mgr.registry.get(job_id)
    if not job or job.execute_kind not in _TRIAGE_KINDS:
        return HTMLResponse("")
    with job._lock:
        cands = list(job.candidates)
    by_artist: dict = {}
    for c in cands:
        by_artist.setdefault(c.get("artist"), []).append(c)
    fresh = [a for a, items in by_artist.items()
             if any(c.get("seq", -1) > after for c in items)]
    if not fresh:
        return HTMLResponse("")
    tmpl = templates.env.get_template("_review_group.html")
    parts = [tmpl.render(job=job, artist=a, items=by_artist[a],
                         triage=True, open=False) for a in fresh]
    return HTMLResponse("\n".join(parts))


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
        new_job = job_mgr.Job(title=title, artist=artist, album_id=album_id)
        job_mgr.submit(new_job, _make_download_run(album, token))
        return RedirectResponse(url=f"/jobs/{new_job.id}", status_code=303)
    except (SystemExit, NoCredsError):
        return RedirectResponse(url="/settings?error=creds", status_code=303)
    except Exception:
        return RedirectResponse(
            url="/queue?error=" + urllib.parse.quote("Retry failed — check your token."),
            status_code=303,
        )


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(request: Request, job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        return RedirectResponse(url="/queue", status_code=303)
    was_review = job.status == job_mgr.JobStatus.AWAITING_REVIEW
    job_mgr.request_cancel(job)
    # A review discard is instant → queue. A running/scanning job stops
    # cooperatively → keep them on the job page to watch it wind down.
    dest = "/queue" if was_review else f"/jobs/{job_id}"
    return RedirectResponse(url=dest, status_code=303)


@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, error: str = ""):
    return _tr(request, "queue.html", {
        "pending": job_mgr.registry.pending_and_running(),
        "finished": job_mgr.registry.finished(),
        "error": error[:200],
        "page": "queue",
    })


@app.post("/queue/clear")
async def queue_clear():
    """Drop finished/canceled/failed jobs from the registry."""
    job_mgr.registry.clear_finished()
    return RedirectResponse(url="/queue", status_code=303)


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
                       auth_token_prefill="", diagnostics=None):
    from qobuz_librarian.web import settings_store
    creds = _read_creds()
    values = settings_store.current()
    # If credentials come from QOBUZ_USER_AUTH_TOKEN env, anything saved
    # via the form is overridden on next process start — let the user know.
    import os
    creds_from_env = bool(os.environ.get("QOBUZ_USER_AUTH_TOKEN"))
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


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request, user_id: str = Form(""), auth_token: str = Form("")):
    import os
    global _TOKEN_VALID
    loop = asyncio.get_running_loop()
    diags = await loop.run_in_executor(None, _diagnostics)
    existing = _read_creds()
    # First-run with empty inputs: nothing to save and no creds to keep —
    # bounce back with a banner rather than writing blanks and flashing green.
    if not auth_token.strip() and not user_id.strip() \
            and not existing.get("auth_token") \
            and not os.environ.get("QOBUZ_USER_AUTH_TOKEN"):
        return RedirectResponse(url="/settings?error=empty", status_code=303)
    # Blank means "keep the existing value" — the fields are not pre-filled,
    # so an empty submission must not wipe a previously-saved credential.
    if not auth_token.strip() and not user_id.strip() and os.environ.get("QOBUZ_USER_AUTH_TOKEN"):
        # Nothing changed and env creds are already synced at startup — skip
        # the write so a read-only config volume doesn't surface a false error.
        return RedirectResponse(url="/settings?connected=1", status_code=303)
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
        return _settings_response(request, error="rejected",
                                  user_id=user_id.strip(),
                                  auth_token_prefill=_mask_token(auth_token.strip()),
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
        values = {k: True for k in settings_store.BEHAVIOR_KEYS if k in form}
    # Text/enum/list fields: take whatever the form posted; absent =
    # leave unchanged (don't wipe a previously-set value).
    for k in settings_store.TEXT_KEYS:
        if k in form:
            values[k] = form.get(k, "")
    ok = settings_store.save(values)
    # Applied in-memory regardless; error only means it won't persist.
    if not ok:
        return RedirectResponse(url="/settings?error=persist", status_code=303)
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


def _job_to_dict(job, *, log_tail: int = 50, with_candidates: bool = False):
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
    if with_candidates and getattr(job, "candidates", None):
        out["candidates"] = job.candidates
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


def _mask_token(token: str) -> str:
    """Show only the first 8 and last 4 chars — enough to verify a paste
    without exposing the full credential in the page source."""
    if len(token) <= 12:
        return token
    return token[:8] + "•" * min(len(token) - 12, 20) + token[-4:]


def _last_scan_age() -> str | None:
    """Human-readable age of the last library/artist scan, or None."""
    import time as _time
    try:
        ts = float(cfg.LAST_SCAN_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    age = _time.time() - ts
    if age < 120:
        return "just now"
    if age < 3600:
        return f"{int(age / 60)} min ago"
    if age < 86400:
        return f"{int(age / 3600)} hr ago"
    days = int(age / 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def _no_creds_response(request):
    """Return a 303 redirect (or htmx fragment) when no credentials are set."""
    if _is_htmx(request):
        return HTMLResponse(
            '<div class="alert alert-error">No Qobuz credentials set — '
            'visit <a href="/settings" class="link">Settings</a>.</div>',
            status_code=200)
    return RedirectResponse(url="/settings", status_code=303)


_creds_cache: dict | None = None


def _read_creds():
    global _creds_cache
    import os
    env_token = os.environ.get("QOBUZ_USER_AUTH_TOKEN", "")
    if env_token:
        env_uid = os.environ.get("QOBUZ_USER_ID", "")
        return {"user_id": env_uid, "auth_token": env_token}
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
