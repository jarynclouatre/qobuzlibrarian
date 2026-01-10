"""Runtime configuration.

Every value here is overridable via an environment variable of the same
name; the literal in this file is just the fallback when the env is
unset. `compose.yaml` sets the ones a container deployment needs.
"""
import os
from pathlib import Path


def _env(key, default):
    """Return env var cast to the same type as default, or default if unset."""
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return type(default)(val)
    except (ValueError, TypeError):
        return default


def _env_bool(key, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes")


def _env_path(key, default: Path) -> Path:
    val = os.environ.get(key)
    return Path(val) if val else default


# ── Auth — direct env-var override (no streamrip config file needed) ──────────
# Set these in compose.yaml or the Settings page writes them to the config file.
QOBUZ_USER_AUTH_TOKEN = os.environ.get("QOBUZ_USER_AUTH_TOKEN", "")
QOBUZ_USER_ID         = os.environ.get("QOBUZ_USER_ID", "")

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME             = Path.home()

# streamrip config — inside the container at /config/streamrip/config.toml,
# falls back to the host location for dev/CLI usage outside Docker.
STREAMRIP_CONFIG = _env_path(
    "STREAMRIP_CONFIG",
    HOME / ".config" / "streamrip" / "config.toml",
)

MUSIC_ROOT  = _env_path(
    "MUSIC_ROOT",
    Path("/music") if os.environ.get("QF_IN_CONTAINER") else HOME / "Music",
)

def _sibling_of_music(name: str) -> Path:
    """A sibling directory of MUSIC_ROOT — but if MUSIC_ROOT is at the
    filesystem root (e.g. /music → /), put the sibling under ~/.cache
    instead of polluting /. Used for STAGING_DIR / UPGRADE_BACKUP_DIR
    defaults when running outside Docker without explicit env vars."""
    parent = MUSIC_ROOT.parent
    if parent == Path("/"):
        return HOME / ".cache" / "qobuz-librarian" / name
    return parent / name

STAGING_DIR = _env_path("STAGING_DIR", _sibling_of_music(".staging"))

# Beets — bundled inside the container (compose.yaml sets these to
# /config/beets). The fallback is beets' own standard config location, so
# a bare-metal / `pip install` run finds an existing beets setup.
BEETS_CONFIG_DIR = _env_path(
    "BEETS_CONFIG_DIR",
    HOME / ".config" / "beets",
)
BEETS_DB_PATH = _env_path(
    "BEETS_DB_PATH",
    BEETS_CONFIG_DIR / "musiclibrary.db",
)
# Used by beets.py when writing the import override config.
BEETS_STAGING_INSIDE = os.environ.get("BEETS_STAGING_INSIDE", str(STAGING_DIR))

# Optional beets path/naming overrides. Empty = leave beets to its own
# config.yaml (fully editable in the /config volume) / built-in defaults.
# Set these to control folder/file structure without hand-editing YAML,
# e.g. BEETS_PATH_DEFAULT="$albumartist/$album ($year)/$track - $title".
BEETS_PATH_DEFAULT   = os.environ.get("BEETS_PATH_DEFAULT", "").strip()
BEETS_PATH_SINGLETON = os.environ.get("BEETS_PATH_SINGLETON", "").strip()
BEETS_PATH_COMP      = os.environ.get("BEETS_PATH_COMP", "").strip()

# Comma-separated list of beets plugins to enable. Empty = honour whatever
# is set in /config/beets/config.yaml (which seeds with `fetchart` only).
# Override per-deployment without editing the user's config.yaml — e.g.
# BEETS_PLUGINS="fetchart,lastgenre,replaygain,scrub". The override yaml
# emits `plugins: [...]` so this REPLACES whatever the user's config
# declares; leave unset to let their config win.
BEETS_PLUGINS = [p.strip() for p in
                 os.environ.get("BEETS_PLUGINS", "").split(",")
                 if p.strip()]

# COMPOSE_FILE is only used by the legacy docker-exec beets fallback (when
# `beet` isn't on PATH and a compose file exists — never the case in the
# bundled image, where beets is called directly). Default is a CWD-relative
# name so the fallback simply stays inactive unless a user opts in via env.
COMPOSE_FILE = _env_path(
    "COMPOSE_FILE",
    Path("compose.yaml"),
)

# State/data files — all live under DATA_DIR so one volume covers them.
# Default to ~/.local/share/qobuz-librarian (XDG) outside Docker; the
# container sets DATA_DIR=/data so this default is only used by CLI dev
# installs (pipx / pip install -e).
_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or (HOME / ".local" / "share"))
DATA_DIR = _env_path(
    "DATA_DIR",
    _XDG_DATA / "qobuz-librarian",
)

FETCH_LOG_FILE       = DATA_DIR / ".qobuz_fetch_log.json"
APP_LOG_FILE         = DATA_DIR / "qobuz-librarian.log"
LOG_LEVEL            = os.environ.get("LOG_LEVEL", "INFO")
WALK_SEEN_FILE       = DATA_DIR / ".qobuz_walk_seen.txt"
ALBUM_WALK_SEEN_FILE = DATA_DIR / ".qobuz_album_walk_seen.txt"
PENDING_QUEUE_FILE   = DATA_DIR / ".qobuz_pending_queue.json"
LYRIC_RETRY_FILE     = DATA_DIR / ".qobuz_lyric_retry.json"
REPAIR_LOG_PATH      = DATA_DIR / ".qobuz_replaced_tracks.log"
CAPPED_FILE          = DATA_DIR / ".qobuz_upgrade_capped.json"
# lyric_fetch's per-track state file. Defaults inside lyric_fetch.py put
# it next to that script — which means /app/.lyric_fetch_state.json in the
# container. After a PUID/PGID drop, /app is root-owned and not writable,
# so the state file can't be saved and "provider-unavailable" retries
# silently never resume. Routing it into DATA_DIR fixes that.
LYRIC_FETCH_STATE_FILE = DATA_DIR / ".lyric_fetch_state.json"

# Lock file lives in DATA_DIR so the web container and a `docker compose
# run` CLI invocation share it via the /data volume (otherwise each
# container has its own /tmp and the lock can't see the other).
LOCK_FILE = _env_path(
    "LOCK_FILE",
    DATA_DIR / "qobuz_librarian.lock",
)
UPGRADE_BACKUP_DIR = _env_path(
    "UPGRADE_BACKUP_DIR",
    _sibling_of_music(".upgrade_backups"),
)

# ── Web UI ────────────────────────────────────────────────────────────────────
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = _env("WEB_PORT", 8666)

# ── Versioned file schemas ────────────────────────────────────────────────────
PENDING_QUEUE_VERSION = 1
LYRIC_RETRY_VERSION   = 1

# ── API ───────────────────────────────────────────────────────────────────────
QOBUZ_API_BASE = os.environ.get("QOBUZ_API_BASE", "https://www.qobuz.com/api.json/0.2")
QOBUZ_APP_ID   = os.environ.get("QOBUZ_APP_ID",   "798273057")

# ── Download quality ──────────────────────────────────────────────────────────
# streamrip quality code: 1=320kbps, 2=CD/16-bit·44.1kHz lossless,
# 3=24-bit ≤96kHz, 4=24-bit ≤192kHz. Default 4: the best master your
# subscription serves — that's what Qobuz is for. Drop to 2 for CD
# lossless if you want smaller files. Passed to `rip -q`.
STREAMRIP_QUALITY = _env("STREAMRIP_QUALITY", 4)

# ── Lyrics ────────────────────────────────────────────────────────────────────
LYRICS_ENABLED   = _env_bool("LYRICS_ENABLED", True)
# How fetched lyrics are written: "embed" (FLAC tag), "sidecar" (.lrc file
# next to the track), or "both".
LYRICS_FORMAT    = os.environ.get("LYRICS_FORMAT", "embed").strip().lower()
# Comma-separated provider names to try, in order. Empty = built-in default.
LYRICS_PROVIDERS = [p.strip() for p in
                    os.environ.get("LYRICS_PROVIDERS", "").split(",")
                    if p.strip()]

# ── Timeouts / delays ─────────────────────────────────────────────────────────
SEARCH_LIMIT     = _env("SEARCH_LIMIT",     8)
RIP_TIMEOUT      = _env("RIP_TIMEOUT",      900)
DELAY_BETWEEN    = _env("DELAY_BETWEEN",    1.0)
# Pause before the next queued album when Qobuz throttling was detected
# in the last rip (vs the normal DELAY_BETWEEN). Tames the error-wave
# pattern on multi-hundred-album queues. Set 0 to disable.
RATE_LIMIT_COOLDOWN = _env("RATE_LIMIT_COOLDOWN", 30.0)
# INACTIVITY timeout for the beets import (seconds of *zero output*),
# NOT a wall-clock cap: a slow-but-progressing import over R2 / a slow
# NAS keeps printing beets progress, so it is never killed no matter how
# long it runs. Only true silence this long means a genuinely hung
# import (DB lock, prompt, deadlocked plugin) — killing it stops the
# single web job worker from freezing forever. 0 disables the guard.
BEETS_TIMEOUT    = _env("BEETS_TIMEOUT",    3600)
ARTIST_API_DELAY = _env("ARTIST_API_DELAY", 0.4)

# Per-request budgets for the web UI's Qobuz API calls (album/search/track
# fetches and the Settings token check). A slow Qobuz response shouldn't
# park a worker thread for minutes — the user gets a clear timeout instead.
WEB_FETCH_TIMEOUT     = _env("QF_WEB_FETCH_TIMEOUT",     12.0)
WEB_TEST_AUTH_TIMEOUT = _env("QF_WEB_TEST_AUTH_TIMEOUT", 8.0)

# ── Fuzzy-match thresholds ────────────────────────────────────────────────────
FUZZY_DIR_THRESH           = _env("FUZZY_DIR_THRESH",           0.78)
DB_ALBUM_THRESH            = _env("DB_ALBUM_THRESH",            0.85)
CONSOLIDATE_THRESH         = _env("CONSOLIDATE_THRESH",         0.70)
ARTIST_NAME_THRESH         = _env("ARTIST_NAME_THRESH",         0.85)
ARTIST_DIR_MATCH_THRESH    = _env("ARTIST_DIR_MATCH_THRESH",    0.65)
AUTO_SAFE_TITLE_SIM_THRESH = _env("AUTO_SAFE_TITLE_SIM_THRESH", 0.85)

# ── Catalog / walk ────────────────────────────────────────────────────────────
LEFTOVER_WARN_LIMIT    = _env("LEFTOVER_WARN_LIMIT",    50)
ARTIST_CATALOG_LIMIT   = _env("ARTIST_CATALOG_LIMIT",   500)
ARTIST_CATALOG_PAGE    = _env("ARTIST_CATALOG_PAGE",    100)
# Hide releases shorter than this in the "missing albums" step of artist
# mode (singles, very small EPs are usually noise — bump if you want them).
MISSING_ALBUMS_MIN_TRACKS = _env("MISSING_ALBUMS_MIN_TRACKS", 4)

# ── Retention windows (days) ──────────────────────────────────────────────────
UPGRADE_BACKUP_RETENTION_DAYS = _env("UPGRADE_BACKUP_RETENTION_DAYS", 7)
CAPPED_RETENTION_DAYS         = _env("CAPPED_RETENTION_DAYS",         90)

# ── Feature flags ─────────────────────────────────────────────────────────────
AUTO_UPGRADE_ENABLED = _env_bool("AUTO_UPGRADE_ENABLED", False)
# Off by default: most people want the file Qobuz delivers. Opt in if you
# prefer to grab hi-res mixes and downsample them to 44.1/48 kHz to save space.
COMPRESS_ENABLED = _env_bool("COMPRESS_ENABLED", False)

# Album-version / library-structure preferences. CLI and web both read these
# so behaviour is identical across interfaces; override via env or Settings.
#   PREFER_HIRES         pick the hi-res master when an album has several versions
#   CONSOLIDATE          after import, merge sibling/duplicate album folders
#   MIGRATE_MULTI_ARTIST move "Primary, Other/<album>" into "Primary/<album>"
# CONSOLIDATE defaults off: it moves/merges folders, which is opinionated for
# someone else's library layout. It's CLI-only and prompts per folder anyway;
# turn it on (env or Settings) if you want it.
PREFER_HIRES         = _env_bool("PREFER_HIRES",         True)
CONSOLIDATE          = _env_bool("CONSOLIDATE",          False)
MIGRATE_MULTI_ARTIST = _env_bool("MIGRATE_MULTI_ARTIST", False)

# ── Audio extensions ──────────────────────────────────────────────────────────
AUDIO_EXTS = (".flac", ".m4a", ".mp3", ".aac", ".ogg", ".opus", ".wav")
