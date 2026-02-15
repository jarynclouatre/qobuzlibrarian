"""FastAPI web application for Qobuz Librarian."""
import asyncio
import concurrent.futures
import html
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

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import NoCredsError
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


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    import logging
    import os
    import shutil
    global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _CLI_MODE
    _log = logging.getLogger("qobuz_librarian")
    from qobuz_librarian.ui_cli.logging import attach_file_handler
    attach_file_handler(cfg.APP_LOG_FILE, cfg.LOG_LEVEL)
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

    import os as _os
    _UNWRITABLE_VOLUMES.clear()
    # Opt-in via env so tests / dev runs that don't have /staging /music
    # mounted don't trip on the gate. The bundled compose sets this to 1.
    if _os.environ.get("QL_CHECK_VOLUMES") == "1":
        for label, path in (("/staging", cfg.STAGING_DIR),
                            ("/music", cfg.MUSIC_ROOT)):
            if not Path(path).exists() or not _os.access(str(path), _os.W_OK):
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
    job_mgr.start_worker()
    if not shutil.which("rip"):
        _log.warning("`rip` (streamrip) not found in PATH — downloads will fail")
    if not shutil.which("beet"):
        _log.warning("`beet` (beets) not found in PATH — imports will fail")
    if not shutil.which("ffprobe"):
        _log.warning("`ffprobe` not found — FLAC validation disabled")
    # Probe the saved token against Qobuz so a stale slot — non-empty but
    # not actually authenticated — surfaces in the dashboard banner rather
    # than failing the user's first search.
    asyncio.create_task(_probe_token())
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
    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.api.client import qobuz_get
    token = creds["auth_token"]
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: qobuz_get(
                    "album/search", {"query": "ok", "limit": 1}, token),
            ),
            timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
        )
        _TOKEN_VALID = True
    except AuthLost:
        _TOKEN_VALID = False
    except Exception:
        # Network blip or Qobuz hiccup — leave the state unverified rather
        # than flip the banner on transient failures.
        pass


app = FastAPI(title="Qobuz Librarian", docs_url=None, redoc_url=None,
              lifespan=_lifespan)

app.add_middleware(CSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(StripServerHeaderMiddleware)

_here = Path(__file__).parent
templates = Jinja2Templates(directory=str(_here / "templates"))

try:
    from importlib.metadata import version as _pkg_version
    _APP_VERSION = _pkg_version("qobuz-librarian")
except Exception:
    _APP_VERSION = "0.1.0"
templates.env.globals["app_version"] = _APP_VERSION
templates.env.globals["repo_url"] = "https://github.com/jarynclouatre/qobuz-librarian"

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


def _tr(request, name, context):
    """TemplateResponse wrapper for Starlette 1.0+ signature.

    The navbar badge is computed once per full-page render and injected via
    context; partial-fragment renders skip this entirely.
    """
    context.setdefault(
        "pending_job_count",
        len(job_mgr.registry.pending_and_running()),
    )
    context.setdefault("cli_mode", _CLI_MODE)
    return templates.TemplateResponse(request=request, name=name, context=context)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _find_job_touching_album(album_id: str):
    """Return a pending/running/awaiting-review job that already covers
    album_id, either as its direct subject or as one of its candidates."""
    for j in job_mgr.registry.pending_and_running():
        if j.album_id == album_id:
            return j
        for cand in (j.candidates or []):
            payload = cand.get("payload") or {}
            if payload.get("album_id") == album_id:
                return j
            qa = (payload.get("candidate") or {}).get("qobuz_album") or {}
            if qa.get("id") == album_id:
                return j
    return None


def _active_job():
    active = (job_mgr.JobStatus.RUNNING, job_mgr.JobStatus.SCANNING)
    running = [j for j in job_mgr.registry.all() if j.status in active]
    if not running:
        return None
    # Prefer a download that's actually writing files over a scan, so the
    # dashboard card mirrors what the user is most likely watching.
    running.sort(key=lambda j: 0 if j.status == job_mgr.JobStatus.RUNNING else 1)
    return running[0]


@app.head("/")
async def dashboard_head():
    """Uptime monitors / curl -I hit HEAD before GET; serve a body-less 200
    so they don't get a 405 and mark the service down."""
    return Response(status_code=200)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from qobuz_librarian import config as _cfg
    from qobuz_librarian.integrations.lyrics import load_lyric_retry
    from qobuz_librarian.ui_cli.prompts import _read_fetch_log
    # tail-only read so a long-running install with a multi-MB fetch log
    # doesn't slurp the whole file on every dashboard load.
    recent = list(reversed(_read_fetch_log(limit_tail=8)))
    # First-run nudge: a fresh install has no Qobuz credentials, so every
    # search/scan would fail with a cryptic error. Surface it up front
    # instead. Filesystem-only check (no network) so it's cheap per load.
    creds_ok = bool(_read_creds().get("auth_token"))
    lyric_retry_count = len(load_lyric_retry()) if _cfg.LYRIC_RETRY_FILE.exists() else 0
    return _tr(request, "index.html", {
        "active_job": _active_job(),
        "pending": job_mgr.registry.pending_and_running(),
        "review": job_mgr.registry.awaiting_review(),
        "recent": recent,
        "creds_ok": creds_ok,
        "creds_token_valid": _TOKEN_VALID,
        "lock_busy_pid": _LOCK_BUSY_PID,
        "lyric_retry_count": lyric_retry_count,
        "page": "dashboard",
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
            from qobuz_librarian.api.auth import AuthLost, QobuzError
            if parsed and parsed[0] == "album":
                try:
                    raw = [await asyncio.wait_for(
                        loop.run_in_executor(
                            None, lambda: get_album(parsed[1], token)
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
                            lambda: search_albums(query, token, limit=cfg.SEARCH_LIMIT),
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
                f'<div class="alert alert-error">{msg}</div>', status_code=400)
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
                f'<div class="alert alert-warning">Already queued — '
                f'<a href="/jobs/{existing.id}" class="link">view job</a>.</div>')
        return RedirectResponse(url=f"/jobs/{existing.id}", status_code=303)
    force_redownload = str(force).strip().lower() in ("1", "true", "yes", "on")
    try:
        token = _get_token()
        from qobuz_librarian.api.search import get_album
        album = get_album(album_id, token)
        if not force_redownload:
            from qobuz_librarian.library.catalog import (
                compute_missing,
                find_album_dir_filesystem,
                find_existing_tracks,
            )
            try:
                album_dir = find_album_dir_filesystem(album)
            except Exception:
                album_dir = None
            if album_dir is not None:
                try:
                    existing_tracks, _ = find_existing_tracks(album)
                except Exception:
                    existing_tracks = []
                qobuz_tracks = (album.get("tracks") or {}).get("items") or []
                # Only block when the album is genuinely complete. A partial
                # album (some tracks present, some missing) must fall through
                # so process_album can gap-fill just the missing tracks rather
                # than forcing a full re-download via force=1.
                complete = bool(existing_tracks and qobuz_tracks) and not (
                    compute_missing(qobuz_tracks, existing_tracks)[0])
                if complete:
                    msg = "You already own this album."
                    if _is_htmx(request):
                        return HTMLResponse(
                            f'<div class="alert alert-warning">{html.escape(msg)} '
                            f'<a href="/" class="link">Open library</a>.</div>')
                    return RedirectResponse(
                        url="/queue?error=" + urllib.parse.quote(msg),
                        status_code=303)
        title  = album.get("title") or "?"
        artist = (album.get("artist") or {}).get("name") or "?"
        job = job_mgr.Job(title=title, artist=artist, album_id=album_id)

        def run(j):
            from qobuz_librarian.modes.process import process_album
            from qobuz_librarian.ui_cli.errors import plural
            from qobuz_librarian.web.flows import build_args
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
                # Album imported but some tracks failed. Status stays
                # DONE so the folder is reachable; the error surfaces
                # the count so a green check isn't lying about completeness.
                j.error = f"{plural(r['n_fail'], 'track')} failed — see job log"

        job_mgr.submit(job, run)
        if _is_htmx(request):
            return _tr(request, "_job_queued.html", {"job": job})
        # Land on the new job's page so the user sees their download starting.
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
    except (SystemExit, NoCredsError):
        msg = "No Qobuz credentials set — visit Settings."
        if _is_htmx(request):
            return HTMLResponse(f'<div class="alert alert-error">{msg}</div>')
        return RedirectResponse(url="/settings?error=creds", status_code=303)
    except Exception as e:
        from qobuz_librarian.api.auth import AuthLost, QobuzError, friendly_qobuz_error
        if isinstance(e, AuthLost):
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
            return HTMLResponse(f'<div class="alert alert-error">{html.escape(user_msg)}</div>')
        msg = urllib.parse.quote(user_msg, safe="")
        return RedirectResponse(url=f"/queue?error={msg}", status_code=303)


@app.get("/artist", response_class=HTMLResponse)
async def artist_page(request: Request, error: str = ""):
    creds_ok = bool(_read_creds().get("auth_token"))
    return _tr(request, "artist.html", {
        "creds_ok": creds_ok, "error": error[:200], "page": "artist",
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
            status_code=400,
        )
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    from qobuz_librarian.web import flows
    job = job_mgr.Job(title="Artist scan", artist=name)
    job_mgr.submit_scan(
        job,
        lambda j: flows.scan_artist(j, name, _get_token()),
        lambda j, chosen: flows.execute_albums(j, chosen, _get_token()),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request):
    creds_ok = bool(_read_creds().get("auth_token"))
    return _tr(request, "library.html", {"creds_ok": creds_ok, "page": "library"})


@app.post("/library")
async def library_scan(request: Request, mode: str = Form("missing_albums")):
    busy = _lock_busy_response(request)
    if busy is not None:
        return busy
    try:
        _get_token()
    except (SystemExit, NoCredsError):
        return _no_creds_response(request)
    from qobuz_librarian.web import flows
    mode_norm = (mode or "").strip().lower()
    partial_only = mode_norm == "partial_fill"
    title = "Library album-fill scan" if partial_only else "Library gap scan"
    job = job_mgr.Job(title=title)
    job_mgr.submit_scan(
        job,
        lambda j: flows.scan_library(j, _get_token(), partial_only=partial_only),
        lambda j, chosen: flows.execute_albums(j, chosen, _get_token()),
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@app.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request):
    creds_ok = bool(_read_creds().get("auth_token"))
    return _tr(request, "upgrade.html", {"creds_ok": creds_ok, "page": "upgrade"})


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
    job_mgr.submit_scan(
        job,
        lambda j: flows.scan_upgrades(j, _get_token()),
        lambda j, chosen: flows.execute_upgrades(j, chosen, _get_token()),
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
    job_mgr.submit_scan(
        job,
        lambda j: flows.scan_repairs(j, _get_token()),
        lambda j, chosen: flows.execute_repairs(j, chosen, _get_token()),
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
    if not job:
        return RedirectResponse(url="/queue", status_code=303)
    return _tr(request, "job.html", {"job": job, "page": "queue",
                                     "approved": approved, "stale": stale,
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
        from qobuz_librarian.api.search import get_album
        album = get_album(album_id, token)
        title = album.get("title") or job.title or "?"
        artist = (album.get("artist") or {}).get("name") or job.artist or "?"
        new_job = job_mgr.Job(title=title, artist=artist, album_id=album_id)

        def run(j):
            from qobuz_librarian.modes.process import process_album
            from qobuz_librarian.ui_cli.errors import plural
            from qobuz_librarian.web.flows import build_args
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
                # Album imported but some tracks failed. Status stays
                # DONE so the folder is reachable; the error surfaces
                # the count so a green check isn't lying about completeness.
                j.error = f"{plural(r['n_fail'], 'track')} failed — see job log"

        job_mgr.submit(new_job, run)
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
                           "detail": f"{p} is not writable by the container user"})
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

    for binary in ("rip", "beet", "ffprobe"):
        found = _sh.which(binary)
        checks.append({"label": f"`{binary}` binary",
                       "ok": bool(found),
                       "detail": found or f"{binary} not on PATH"})

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


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: bool = False,
                        queued: bool = False, auth: str = "",
                        error: str = "", mode: str = ""):
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
        "user_id": creds.get("user_id", ""),
        "auth_token_set": bool(creds.get("auth_token")),
        "creds_from_env": creds_from_env,
        "cli_only_env": cli_only_env,
        "mode_changed": (mode or "").strip().lower(),
        "saved": saved,
        "queued": queued,
        "auth_check": auth,
        "error": error,
        "page": "settings",
        "music_root": cfg.MUSIC_ROOT,
        "staging_dir": cfg.STAGING_DIR,
        "beets_db": cfg.BEETS_DB_PATH,
        "streamrip_config": cfg.STREAMRIP_CONFIG,
        "behavior_fields": settings_store.BEHAVIOR_FIELDS,
        "text_fields": settings_store.TEXT_FIELDS,
        "option_labels": settings_store.ENUM_OPTION_LABELS,
        "behavior": values,
        "diagnostics": _diagnostics(),
    })


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request, user_id: str = Form(""), auth_token: str = Form("")):
    import os
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
        return RedirectResponse(url="/settings?saved=1", status_code=303)
    new_token = auth_token.strip() or existing.get("auth_token", "")
    new_uid = user_id.strip() or existing.get("user_id", "")
    ok = _write_creds(new_uid, new_token)
    if not ok:
        return RedirectResponse(url="/settings?error=creds", status_code=303)
    # Probe the new token so the user finds out at save time, not on their
    # first search. Network/timeout failures stay silent — only an outright
    # AuthLost from Qobuz flips the banner.
    auth_flag = ""
    if new_token:
        from qobuz_librarian.api.auth import AuthLost
        from qobuz_librarian.api.client import qobuz_get
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: qobuz_get("album/search",
                                      {"query": "test", "limit": 1}, new_token),
                ),
                timeout=cfg.WEB_TEST_AUTH_TIMEOUT,
            )
        except AuthLost:
            auth_flag = "&auth=bad"
        except Exception:
            pass
    return RedirectResponse(url=f"/settings?saved=1{auth_flag}",
                            status_code=303)


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
    global _RUN_LOCK_HANDLE, _LOCK_BUSY_PID, _CLI_MODE
    from qobuz_librarian import run_lock
    want = (target or "").strip().lower()
    if want == "cli":
        if job_mgr.registry.pending_and_running():
            return RedirectResponse(url="/settings?error=" + urllib.parse.quote(
                "Finish or cancel the active download before handing off to the "
                "terminal."), status_code=303)
        if _RUN_LOCK_HANDLE is not None:
            try:
                _RUN_LOCK_HANDLE.close()  # closing the handle releases the flock
            except OSError:
                pass
            _RUN_LOCK_HANDLE = None
        _CLI_MODE = True
        _LOCK_BUSY_PID = None
        return RedirectResponse(url="/settings?mode=cli", status_code=303)
    if want == "web":
        try:
            _RUN_LOCK_HANDLE = run_lock.acquire()
            _CLI_MODE = False
            _LOCK_BUSY_PID = None
            return RedirectResponse(url="/settings?mode=web", status_code=303)
        except run_lock.LockBusy:
            # A CLI session still holds the lock — can't take it back yet.
            return RedirectResponse(url="/settings?error=" + urllib.parse.quote(
                "The terminal is still using it — finish your CLI command, then "
                "resume."), status_code=303)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/test-auth", response_class=HTMLResponse)
async def test_auth(request: Request):
    try:
        form = await request.form()
        token = (form.get("auth_token") or "").strip() or _get_token()
        from qobuz_librarian.api.auth import AuthLost, QobuzError
        from qobuz_librarian.api.client import qobuz_get
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: qobuz_get("album/search", {"query": "test", "limit": 1}, token),
            ),
            timeout=cfg.WEB_FETCH_TIMEOUT,
        )
        return HTMLResponse('<div class="alert alert-success py-2">Connected — token is valid. <a href="/" class="link">Go to dashboard</a></div>')
    except asyncio.TimeoutError:
        return HTMLResponse('<div class="alert alert-error py-2">Timed out — Qobuz API unreachable.</div>')
    except (SystemExit, NoCredsError):
        return HTMLResponse('<div class="alert alert-error py-2">No Qobuz credentials set — visit Settings.</div>')
    except AuthLost:
        return HTMLResponse('<div class="alert alert-error py-2">Token is expired or invalid.</div>')
    except QobuzError as e:
        from qobuz_librarian.api.auth import friendly_qobuz_error
        cleaned = friendly_qobuz_error(e)
        if cleaned.startswith("HTTP 401") or cleaned.startswith("HTTP 400"):
            return HTMLResponse(
                '<div class="alert alert-error py-2">Token rejected by Qobuz — '
                're-grab it from play.qobuz.com (dev tools &rarr; Application '
                '&rarr; Local Storage &rarr; localuser &rarr; token).</div>')
        return HTMLResponse('<div class="alert alert-error py-2">Couldn\'t reach the Qobuz API — check the container\'s network.</div>')
    except Exception:
        import logging as _logging
        _logging.getLogger("qobuz_librarian").exception("test-auth crashed")
        return HTMLResponse(
            '<div class="alert alert-error py-2">Test failed — check the container log.</div>')


# Empty 500ms ticks before we emit a `: ping` heartbeat to keep
# reverse proxies from dropping the EventSource on a quiet scan.
# Defaults from cfg.SSE_HEARTBEAT_TICKS / cfg.SSE_MAX_WORKERS (env-tunable).
_SSE_HEARTBEAT_TICKS = cfg.SSE_HEARTBEAT_TICKS

# Dedicated thread pool for SSE waits so a long-running scan with many
# tabs open doesn't starve /search / /download / /api/test-auth on the
# default executor.
_SSE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=cfg.SSE_MAX_WORKERS, thread_name_prefix="sse")


@app.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str):
    job = job_mgr.registry.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def _generator():
        import logging as _logging
        import queue as _queue
        yield "retry: 2000\n\n"
        if (job.status in job_mgr.TERMINAL
                or job.status == job_mgr.JobStatus.AWAITING_REVIEW):
            for line in job.log_lines[-job.REPLAY_TAIL:]:
                escaped = line.replace("\n", " ").replace("\r", "")
                yield f"data: {escaped}\n\n"
            yield "event: done\ndata: done\n\n"
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
                    if line == "__DONE__":
                        yield "event: done\ndata: done\n\n"
                        break
                    escaped = line.replace("\n", " ").replace("\r", "")
                    yield f"data: {escaped}\n\n"
                except _queue.Empty:
                    if (job.status in job_mgr.TERMINAL
                            or job.status == job_mgr.JobStatus.AWAITING_REVIEW):
                        yield "event: done\ndata: done\n\n"
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
    from fastapi.responses import JSONResponse
    wanted = status.strip().lower() or None
    if wanted is not None:
        valid = {s.value for s in job_mgr.JobStatus}
        if wanted not in valid:
            raise HTTPException(status_code=400,
                                detail="Unknown status filter")
    matching = []
    with job_mgr.registry._lock:
        ordered = [job_mgr.registry._jobs[i] for i in job_mgr.registry._order
                   if i in job_mgr.registry._jobs]
    for j in reversed(ordered):
        if wanted and j.status.value != wanted:
            continue
        matching.append(_job_to_dict(j, log_tail=0))
        if len(matching) >= max(1, min(int(limit), 500)):
            break
    return JSONResponse({"jobs": matching, "count": len(matching)})


def _get_token():
    from qobuz_librarian.api.auth import load_qobuz_token
    return load_qobuz_token()[1]


def _no_creds_response(request):
    """Return a 303 redirect (or htmx fragment) when no credentials are set."""
    if _is_htmx(request):
        return HTMLResponse(
            '<div class="alert alert-error">No Qobuz credentials set — '
            'visit <a href="/settings" class="link">Settings</a>.</div>',
            status_code=200)
    return RedirectResponse(url="/settings", status_code=303)


def _read_creds():
    import os
    env_token = os.environ.get("QOBUZ_USER_AUTH_TOKEN", "")
    if env_token:
        env_uid = os.environ.get("QOBUZ_USER_ID", "")
        return {"user_id": env_uid, "auth_token": env_token}
    if not cfg.STREAMRIP_CONFIG.exists():
        return {}
    try:
        import tomllib
        with open(cfg.STREAMRIP_CONFIG, "rb") as f:
            data = tomllib.load(f)
        qz = data.get("qobuz", {})
        return {"user_id": qz.get("email_or_userid", ""), "auth_token": qz.get("password_or_token", "")}
    except Exception:
        return {}


def _write_creds(user_id, auth_token) -> bool:
    """Write credentials into the streamrip config. Returns False if the
    config volume isn't writable (NAS perms) so the Settings page can show
    a clear message rather than 500ing.

    Delegates to qobuz_librarian.api.auth.write_streamrip_creds so the web
    Settings path and the env-var sync share one credential writer."""
    from qobuz_librarian.api.auth import write_streamrip_creds
    return write_streamrip_creds(user_id, auth_token)


def start():
    import uvicorn
    uvicorn.run("qobuz_librarian.web.app:app", host=cfg.WEB_HOST, port=cfg.WEB_PORT, workers=1)
