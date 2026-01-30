"""Persisted behaviour settings for the web UI, layered on top of config.py.

Values are applied to the config module so flows read cfg.* at call time.
During a running job the in-memory apply is deferred until the worker idles
to avoid mid-job quality or config changes; disk write still happens immediately.
"""
import json
import os
import tempfile
import threading
from typing import Optional

from qobuz_fetch import config as cfg

SETTINGS_FILE = cfg.DATA_DIR / ".qobuz_settings.json"

# Set by save() when an active job blocks the in-memory apply; drained by
# drain_pending() once the worker idles. Lock guards the slot since save()
# runs on the web request thread and drain_pending() runs on the worker.
_pending_apply: Optional[dict] = None
_pending_lock = threading.Lock()

# (key, label, help) — display order on the Settings page.
BEHAVIOR_FIELDS = [
    ("PREFER_HIRES", "Prefer Hi-Res",
     "When an album has several versions, pick the highest-quality one. "
     "Off picks the earliest release instead."),
    ("CONSOLIDATE", "Consolidate duplicate folders (CLI only)",
     "After import, merge sibling/duplicate album folders into one. "
     "Consolidation needs interactive per-folder confirmation and only "
     "runs from the CLI; this toggle has no effect on web downloads."),
    ("MIGRATE_MULTI_ARTIST", "Migrate multi-artist folders",
     "Move 'Primary, Other/<album>' into 'Primary/<album>' after import."),
    ("AUTO_UPGRADE_ENABLED", "Offer upgrades during walks",
     "Let ordinary gap-fill walks also surface quality upgrades. The "
     "explicit Upgrade scan always works regardless of this."),
    ("DOWNSAMPLE_HIRES_ENABLED", "Downsample hi-res before import",
     "Resample 88.2/96 kHz+ FLACs to 44.1/48 kHz to save space."),
    ("LYRICS_ENABLED", "Fetch lyrics",
     "Look up synced/plain lyrics on import."),
]
BEHAVIOR_KEYS = [k for k, _, _ in BEHAVIOR_FIELDS]

# (key, label, help, kind, choices, placeholder).
# kind: "text" — free string; "enum" — choices is a list of allowed values;
# "list" — comma-separated; stored as list on cfg.
TEXT_FIELDS = [
    ("STREAMRIP_QUALITY", "Download quality",
     "A ceiling, not a guarantee: Qobuz delivers the highest quality it has for "
     "each release, up to this. Hi-res isn't available for every album.",
     "enum", ["4", "3", "2", "1"], ""),
    ("LYRICS_FORMAT", "Lyrics format",
     "How lyrics are written when fetched.",
     "enum", ["embed", "sidecar", "both"], ""),
    ("ARTWORK", "Album art",
     "Where cover art goes: a file in the album folder (sidecar), embedded in "
     "the track tags with no leftover file (embed), or both.",
     "enum", ["sidecar", "embed", "both"], ""),
    ("LYRICS_PROVIDERS", "Lyrics providers",
     "Comma-separated list of providers to try in order. "
     "Available: Lrclib, NetEase, Musixmatch. Empty = built-in default (all three).",
     "list", None, "e.g. Lrclib, NetEase"),
    ("BEETS_PATH_DEFAULT", "beets path: default",
     "Folder/file naming for normal albums (beets path syntax). "
     "Empty = use whatever is in beets/config.yaml "
     "(check /config/beets/config.yaml to see the current value).",
     "text", None, "e.g. $albumartist/$album ($year)/$track - $title"),
    ("BEETS_PATH_SINGLETON", "beets path: singleton",
     "Naming for singleton tracks. Empty = beets default.",
     "text", None, "e.g. $albumartist/$album ($year)/$track - $title"),
    ("BEETS_PATH_COMP", "beets path: compilation",
     "Naming for compilations / Various Artists. Empty = beets default.",
     "text", None, "e.g. Various Artists/$album ($year)/$track - $title"),
    ("BEETS_PLUGINS", "beets plugins",
     "Comma-separated list of beets plugins to enable. Replaces the list "
     "in /config/beets/config.yaml entirely. Empty = honour that file "
     "(seeded with fetchart only). Examples: lastgenre, replaygain, "
     "scrub, edit.",
     "list", None, "fetchart,lastgenre,replaygain"),
]
TEXT_KEYS = [k for k, *_ in TEXT_FIELDS]

# Friendlier dropdown text for enum values whose bare value isn't self-explaining;
# falls back to the raw value for anything not listed.
ENUM_OPTION_LABELS = {
    "STREAMRIP_QUALITY": {
        "4": "4 — 24-bit ≤192 kHz",
        "3": "3 — 24-bit ≤96 kHz",
        "2": "2 — 16-bit / 44.1 kHz",
        "1": "1 — 320 kbps MP3",
    },
}


def _list_to_str(v) -> str:
    if isinstance(v, list):
        return ",".join(v)
    return str(v or "")


def _str_to_list(s: str) -> list:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def current() -> dict:
    """Live value of every persisted setting, as seen by the cfg module.

    If a save() was deferred while a job was running, the pending values
    are overlaid so the Settings page reflects what the user saved rather
    than the still-unchanged cfg.* values. The cfg.* read happens inside
    _pending_lock so a concurrent drain_pending (which holds the lock
    across its _apply) can't slip cfg.* updates in between the cfg read
    and the overlay read — that gap silently dropped the pending change.
    """
    with _pending_lock:
        out = {k: bool(getattr(cfg, k)) for k in BEHAVIOR_KEYS}
        # DOWNSAMPLE_HIRES_ENABLED replaced COMPRESS_ENABLED but the
        # legacy attribute still gets set directly by older callers and
        # tests. Surface whichever side is currently True so the Settings
        # page reflects user intent across both names.
        if "DOWNSAMPLE_HIRES_ENABLED" in out:
            out["DOWNSAMPLE_HIRES_ENABLED"] = bool(
                out.get("DOWNSAMPLE_HIRES_ENABLED")
                or getattr(cfg, "COMPRESS_ENABLED", False)
            )
        for key, _, _, kind, _, _ in TEXT_FIELDS:
            v = getattr(cfg, key, "")
            out[key] = _list_to_str(v) if kind == "list" else str(v or "")
        if _pending_apply:
            for k in BEHAVIOR_KEYS:
                if k in _pending_apply:
                    out[k] = bool(_pending_apply[k])
            for key, _, _, kind, _, _ in TEXT_FIELDS:
                if key in _pending_apply:
                    v = _pending_apply[key]
                    out[key] = _list_to_str(v) if kind == "list" else str(v or "")
    return out


def _apply(values: dict):
    # Accept either DOWNSAMPLE_HIRES_ENABLED (the canonical name) or the
    # legacy COMPRESS_ENABLED key from on-disk settings files. Whichever
    # the user supplied wins; both cfg attributes get set so old and new
    # call sites see the same value.
    if ("DOWNSAMPLE_HIRES_ENABLED" not in values
            and "COMPRESS_ENABLED" in values):
        values = dict(values)
        values["DOWNSAMPLE_HIRES_ENABLED"] = bool(values["COMPRESS_ENABLED"])
    for k in BEHAVIOR_KEYS:
        if k in values:
            setattr(cfg, k, bool(values[k]))
    # Mirror the downsample flag onto its legacy attribute so code paths
    # that still read cfg.COMPRESS_ENABLED keep seeing the right value.
    if "DOWNSAMPLE_HIRES_ENABLED" in values:
        setattr(cfg, "COMPRESS_ENABLED",
                bool(values["DOWNSAMPLE_HIRES_ENABLED"]))
    for key, _, _, kind, choices, _ in TEXT_FIELDS:
        if key not in values:
            continue
        raw = values[key]
        if kind == "list":
            setattr(cfg, key, _str_to_list(raw) if isinstance(raw, str)
                    else list(raw or []))
        elif kind == "enum":
            v = str(raw or "").strip().lower()
            if choices and v not in choices:
                continue  # ignore garbage, keep current
            # STREAMRIP_QUALITY is sourced from the env loader as int —
            # keep the type stable so cfg.STREAMRIP_QUALITY is always int,
            # not str on the post-save path.
            if key == "STREAMRIP_QUALITY":
                try:
                    setattr(cfg, key, int(v))
                except ValueError:
                    continue
            else:
                setattr(cfg, key, v)
        else:
            setattr(cfg, key, str(raw or "").strip())

    # The streamrip quality cap is cached after first use; if the quality
    # just changed, drop the cache so the upgrade scanner re-derives it.
    if "STREAMRIP_QUALITY" in values:
        from qobuz_fetch.quality.tiers import reset_streamrip_cap_cache
        reset_streamrip_cap_cache()


def load():
    """Apply the persisted settings file over env defaults, if present."""
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _apply(data)
    except (OSError, ValueError):
        pass


def _any_active_job() -> bool:
    """True if any job is currently scanning/running/awaiting review.

    Late import so settings_store can be loaded without jobs.py being
    importable yet (eg during CLI startup that never touches the web).
    """
    try:
        from qobuz_fetch.web import jobs as job_mgr
    except ImportError:
        return False
    return bool(job_mgr.registry.pending_and_running()
                or job_mgr.registry.awaiting_review())


def drain_pending():
    """Apply any settings change that was deferred because a job was
    running. Called by the worker loop after each task completes.

    Holds _pending_lock across _apply so a concurrent save()'s `current()`
    read either sees the overlay AND blocks on the lock, or sees the
    already-applied cfg.* values — never the gap in between, which would
    silently drop the pending change.
    """
    global _pending_apply
    with _pending_lock:
        pending = _pending_apply
        _pending_apply = None
        if pending is not None:
            _apply(pending)


_save_lock = threading.Lock()


def save(values: dict) -> bool:
    """Apply settings and persist them atomically.

    If a job is already active, the in-memory apply is deferred until the
    worker idles (drain_pending). Persistence to disk still happens
    immediately so the new values survive a restart. Returns False only
    if persistence failed. The whole read-merge-write is serialised so
    concurrent saves don't lose each other's keys.
    """
    with _save_lock:
        return _save_locked(values)


def _save_locked(values: dict) -> bool:
    merged = current()
    # Map the legacy COMPRESS_ENABLED key onto the canonical name so
    # callers passing the old key still flip the flag.
    if "COMPRESS_ENABLED" in values and "DOWNSAMPLE_HIRES_ENABLED" not in values:
        values = dict(values)
        values["DOWNSAMPLE_HIRES_ENABLED"] = bool(values["COMPRESS_ENABLED"])
    for k in BEHAVIOR_KEYS:
        if k in values:
            merged[k] = bool(values[k])
    for key, _, _, kind, choices, _ in TEXT_FIELDS:
        if key not in values:
            continue
        # Enum fields are validated here too (not just in _apply), so a
        # forged POST can't persist an out-of-range value the loader would
        # silently reject — the on-disk file stays consistent with cfg.
        if kind == "enum" and choices:
            v = str(values[key] or "").strip().lower()
            if v not in choices:
                continue
        merged[key] = values[key]

    if _any_active_job():
        with _pending_lock:
            global _pending_apply
            _pending_apply = merged
    else:
        _apply(merged)

    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_FILE.parent),
                                   prefix=".qobuz_settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
            os.replace(tmp, SETTINGS_FILE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except OSError:
        return False
