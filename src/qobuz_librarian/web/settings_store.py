"""Persisted behaviour settings for the web UI, layered on top of config.py.

Values are applied to the config module so flows read cfg.* at call time.
During a running job the in-memory apply is deferred until the worker idles
to avoid mid-job quality or config changes; disk write still happens immediately.
"""
import json
import logging
import os
import tempfile
import threading
from typing import Optional

from qobuz_librarian import config as cfg

log = logging.getLogger("qobuz_librarian")

SETTINGS_FILE = cfg.DATA_DIR / ".qobuz_settings.json"

# Set by save() when an active job blocks the in-memory apply; drained by
# drain_pending() once the worker idles. Lock guards the slot since save()
# runs on the web request thread and drain_pending() runs on the worker.
_pending_apply: Optional[dict] = None
_pending_lock = threading.Lock()

# (key, label, help) — display order on the Settings page.
BEHAVIOR_FIELDS = [
    ("PREFER_HIRES", "Prefer Hi-Res",
     "When an album comes in several editions, ON picks the standard album at "
     "the best available quality (may be remastered). OFF picks the original "
     "release (may already be Hi-Res). Mainly affects which version the "
     "library and artist scans suggest."),
    ("CONSOLIDATE", "Consolidate duplicate folders (CLI only)",
     "After import, merge sibling/duplicate album folders into one. "
     "Consolidation needs interactive per-folder confirmation and only "
     "runs from the CLI; this toggle has no effect on web downloads."),
    ("MIGRATE_MULTI_ARTIST", "Migrate multi-artist folders",
     "When an album folder is named after several artists "
     "('Louis Prima, Sam Butera/'), file it under just the main artist "
     "('Louis Prima/') after import."),
    ("AUTO_UPGRADE_ENABLED", "Offer upgrades during walks",
     "Let ordinary gap-fill walks also surface quality upgrades. The "
     "explicit Upgrade scan always works regardless of this."),
    ("DOWNSAMPLE_HIRES_ENABLED", "Downsample hi-res before import",
     "Resample hi-res FLACs down to 44.1 or 48 kHz (whichever fits the "
     "source) to save space."),
    ("LYRICS_ENABLED", "Fetch lyrics",
     "Fetch and save synced/plain lyrics on import."),
]
BEHAVIOR_KEYS = [k for k, _, _ in BEHAVIOR_FIELDS]

# Provider names lyric_fetch.py knows how to drive (mirrors the provider list
# it wires up). Used to validate the Lyrics providers field: entries are
# matched case-insensitively and normalised to these spellings; anything else
# is dropped so a typo can't silently turn lyric fetching off.
LYRICS_PROVIDER_CHOICES = [
    "Lrclib", "NetEase", "Megalobiz", "Musixmatch", "Genius",
]

# (key, label, help, kind, choices, placeholder).
# kind: "text" — free string; "enum" — choices is a list of allowed values;
# "list" — comma-separated; stored as list on cfg. A list field with choices
# is validated against them; choices=None means any entry is accepted.
TEXT_FIELDS = [
    ("STREAMRIP_QUALITY", "Download quality",
     "Sets the maximum quality to request. Qobuz serves the best it has for "
     "each album up to this — hi-res isn't available for everything.",
     "enum", ["4", "3", "2", "1"], ""),
    ("LYRICS_FORMAT", "Lyrics format",
     "How lyrics are written when fetched.",
     "enum", ["embed", "sidecar", "both"], ""),
    ("ARTWORK", "Album art",
     "Where cover art goes: a file in the album folder (sidecar), embedded in "
     "the track tags with no leftover file (embed), or both.",
     "enum", ["sidecar", "embed", "both"], ""),
    ("LYRICS_PROVIDERS", "Lyrics providers",
     "Comma-separated list of providers to try in order. Available: Lrclib, "
     "NetEase, Megalobiz, Musixmatch, Genius. Unknown names are ignored. "
     "Empty = default (Lrclib, NetEase, Musixmatch).",
     "list", LYRICS_PROVIDER_CHOICES, "e.g. Lrclib, NetEase"),
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
     "(seeded with fetchart only). Names that aren't installed here are "
     "dropped (and called out) so a typo can't break every import. "
     "Examples: lastgenre, replaygain, scrub, edit.",
     "list", None, "fetchart,lastgenre,replaygain"),
    ("ARTIST_CATALOG_CACHE_TTL", "Album-list freshness",
     "How long the library and artist gap scans reuse a fetched discography "
     "before asking Qobuz again. The new-release check always fetches fresh "
     "regardless, so this only trades gap-scan speed against how current its "
     "album lists are.",
     "enum", ["86400", "259200", "604800", "2592000"], ""),
    ("NEW_RELEASE_CHECK_INTERVAL", "Auto-check for new releases",
     "When you open the app and it's been at least this long since the last "
     "check (and nothing's already scanning), it quietly looks for new releases "
     "in the background and shows them on the dashboard to review. It never "
     "downloads on its own. Off = only the manual buttons.",
     "enum", ["0", "21600", "43200", "86400", "604800"], ""),
]
TEXT_KEYS = [k for k, *_ in TEXT_FIELDS]

# Enum fields whose value is an int on cfg (the form/JSON carry strings).
_INT_ENUM_KEYS = {"STREAMRIP_QUALITY", "ARTIST_CATALOG_CACHE_TTL",
                  "NEW_RELEASE_CHECK_INTERVAL"}

# Friendlier dropdown text for enum values whose bare value isn't self-explaining;
# falls back to the raw value for anything not listed.
ENUM_OPTION_LABELS = {
    "STREAMRIP_QUALITY": {
        "4": "24-bit ≤192 kHz",
        "3": "24-bit ≤96 kHz",
        "2": "16-bit / 44.1 kHz",
        "1": "320 kbps MP3",
    },
    "ARTIST_CATALOG_CACHE_TTL": {
        "86400": "1 day",
        "259200": "3 days",
        "604800": "7 days (default)",
        "2592000": "30 days",
    },
    "NEW_RELEASE_CHECK_INTERVAL": {
        "0": "Off",
        "21600": "Every 6 hours",
        "43200": "Every 12 hours",
        "86400": "Daily (default)",
        "604800": "Weekly",
    },
}


def _list_to_str(v) -> str:
    if isinstance(v, list):
        return ",".join(v)
    return str(v or "")


def _field_str(v, kind) -> str:
    """Stringify a setting for the form/template. int-valued enums keep 0 as
    "0" (a real choice, e.g. the auto-check's Off) rather than collapsing it to
    the empty string the `v or ""` shortcut would produce."""
    if kind == "list":
        return _list_to_str(v)
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return str(v or "")


def _str_to_list(s: str) -> list:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def _normalize_list_choices(items, choices):
    """Keep only entries naming a known choice, normalised to its canonical
    spelling (case-insensitive). Drops unknowns and de-dupes, preserving order.
    Returns (kept, dropped)."""
    canon = {c.lower(): c for c in choices}
    kept, dropped = [], []
    for it in items:
        c = canon.get(str(it).strip().lower())
        if c is None:
            dropped.append(str(it).strip())
        elif c not in kept:
            kept.append(c)
    return kept, dropped


def _available_beets_plugins():
    """Plugin names beets can actually import on this server (submodules of the
    beetsplug namespace). Returns None when the set can't be determined — beets
    isn't importable from here, or enumeration failed — so the caller validates
    nothing rather than dropping every plugin the user typed."""
    try:
        import pkgutil

        import beetsplug
    except Exception:
        return None
    try:
        names = {m.name for m in pkgutil.iter_modules(beetsplug.__path__)
                 if not m.name.startswith("_")}
    except Exception:
        return None
    return names or None


def _validate_list(key, items):
    """Sanitise a comma-separated list setting. Returns (kept, dropped).

    LYRICS_PROVIDERS is matched against the providers lyric_fetch can drive;
    BEETS_PLUGINS against the plugins beets can actually load here — a name
    that can't load would otherwise break every import silently. Both
    normalise spelling case-insensitively, drop unknowns, and de-dupe. Fields
    with no known set (free-text lists) just de-dupe."""
    if key == "LYRICS_PROVIDERS":
        return _normalize_list_choices(items, LYRICS_PROVIDER_CHOICES)
    if key == "BEETS_PLUGINS":
        avail = _available_beets_plugins()
        if avail:
            return _normalize_list_choices(items, sorted(avail))
        return list(dict.fromkeys(i.strip() for i in items if i.strip())), []
    return list(dict.fromkeys(i.strip() for i in items if i.strip())), []


def _dropped_warning(key, dropped):
    names = ", ".join(dropped)
    if key == "LYRICS_PROVIDERS":
        return (f"Ignored unrecognised lyrics provider(s): {names}. "
                f"Known providers are {', '.join(LYRICS_PROVIDER_CHOICES)}.")
    if key == "BEETS_PLUGINS":
        return (f"Ignored beets plugin(s) not installed on this server: {names}. "
                "They were dropped so imports keep working — check the spelling, "
                "or install them and add them back.")
    return f"Ignored unrecognised value(s) for {key}: {names}."


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
            out[key] = _field_str(getattr(cfg, key, ""), kind)
        if _pending_apply:
            for k in BEHAVIOR_KEYS:
                if k in _pending_apply:
                    out[k] = bool(_pending_apply[k])
            for key, _, _, kind, _, _ in TEXT_FIELDS:
                if key in _pending_apply:
                    out[key] = _field_str(_pending_apply[key], kind)
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
            items = _str_to_list(raw) if isinstance(raw, str) else list(raw or [])
            items, _ = _validate_list(key, items)
            setattr(cfg, key, items)
        elif kind == "enum":
            v = str(raw or "").strip().lower()
            if choices and v not in choices:
                continue  # ignore garbage, keep current
            # A few enums are ints on cfg (quality tier, cache seconds) — keep
            # the type stable so cfg.* stays int, not str, on the post-save path.
            if key in _INT_ENUM_KEYS:
                try:
                    setattr(cfg, key, int(v))
                except ValueError:
                    continue
            else:
                setattr(cfg, key, v)
        else:
            setattr(cfg, key, str(raw or "").strip())


def load():
    """Apply the persisted settings file over env defaults, if present."""
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _apply(data)
    except (OSError, ValueError) as exc:
        # A corrupt or unreadable file would otherwise revert every behaviour
        # setting to its env default with no hint why — say so once.
        log.warning("Ignoring unreadable settings file %s: %s",
                    SETTINGS_FILE, exc)


def _any_active_job() -> bool:
    """True if any job is currently scanning/running/awaiting review.

    Late import so settings_store can be loaded without jobs.py being
    importable yet (eg during CLI startup that never touches the web).
    """
    try:
        from qobuz_librarian.web import jobs as job_mgr
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


def save(values: dict):
    """Apply settings and persist them atomically. Returns (ok, warnings).

    If a job is already active, the in-memory apply is deferred until the
    worker idles (drain_pending). Persistence to disk still happens
    immediately so the new values survive a restart. `ok` is False only if
    persistence failed; `warnings` lists any list-field entries that were
    dropped as unknown (e.g. a misspelt lyrics provider or an uninstalled
    beets plugin) so the caller can tell the user instead of silently eating
    them. The whole read-merge-write is serialised so concurrent saves don't
    lose each other's keys.
    """
    with _save_lock:
        return _save_locked(values)


def _save_locked(values: dict):
    merged = current()
    warnings = []
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
        # Enum/list values are validated here too (not just in _apply), so a
        # forged POST can't persist a value the loader would reject — the
        # on-disk file stays consistent with cfg.
        if kind == "enum" and choices:
            v = str(values[key] or "").strip().lower()
            if v not in choices:
                continue
            merged[key] = v  # persist the normalised value, matching cfg
            continue
        elif kind == "list":
            raw = values[key]
            items = _str_to_list(raw) if isinstance(raw, str) else list(raw or [])
            kept, dropped = _validate_list(key, items)
            merged[key] = ",".join(kept)
            if dropped:
                warnings.append(_dropped_warning(key, dropped))
            continue
        merged[key] = values[key]

    with _pending_lock:
        global _pending_apply
        if _any_active_job():
            _pending_apply = merged
        else:
            # merged already folds in any deferred change (current() overlaid
            # it), so applying it now supersedes _pending_apply — clear it so a
            # drain firing right after can't roll those fields back to the old
            # deferred copy. Apply under the lock, like drain_pending does.
            _pending_apply = None
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
        return True, warnings
    except OSError:
        return False, warnings
