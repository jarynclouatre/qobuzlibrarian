"""State for the new-release quickscan.

The quickscan compares each library artist's current Qobuz catalog against the
album ids recorded here from the last check; anything new the user doesn't own
and hasn't hidden is a new release. The first check of an artist records only a
baseline (so the back catalogue isn't dumped as "new") — later checks surface
the difference. ``last_run`` lets the dashboard throttle the automatic check.
"""
import json
import time

from qobuz_librarian import config as cfg


def load() -> dict:
    """Return ``{"last_run": float|None, "seen": {artist_id: [album_id, …]},
    "baseline_complete": bool}``, tolerating a missing or corrupt file with an
    empty baseline."""
    base = {"last_run": None, "seen": {}, "baseline_complete": False}
    try:
        data = json.loads(cfg.NEW_RELEASE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return base
    if not isinstance(data, dict):
        return base
    seen = data.get("seen")
    if isinstance(seen, dict):
        base["seen"] = {str(k): v for k, v in seen.items() if isinstance(v, list)}
    lr = data.get("last_run")
    if isinstance(lr, (int, float)):
        base["last_run"] = float(lr)
    base["baseline_complete"] = bool(data.get("baseline_complete"))
    return base


def save(state) -> None:
    try:
        cfg.NEW_RELEASE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.NEW_RELEASE_STATE_FILE.with_suffix(
            cfg.NEW_RELEASE_STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(cfg.NEW_RELEASE_STATE_FILE)
    except OSError:
        pass


def last_run() -> float | None:
    return load().get("last_run")


def mark_run(seen, when=None) -> None:
    """Persist the updated per-artist catalog snapshot and the run time, keeping
    the baseline_complete flag (load-update-save, not a fresh dict)."""
    state = load()
    state["seen"] = seen
    state["last_run"] = when if when is not None else time.time()
    save(state)


def is_baseline_complete() -> bool:
    """True once a full library scan has established the baseline. The automatic
    new-release check stays dormant until then — so it never crawls to an empty
    baseline (surfacing nothing) or activates off a partial scan."""
    return bool(load().get("baseline_complete"))


def seed_baseline(seen) -> None:
    """Record the per-artist catalog snapshot from a cleanly-completed library
    scan and mark the baseline ready. The scan already fetched every discography,
    so this captures "what exists now" for free; the daily check diffs against it."""
    state = load()
    state["seen"] = {str(k): list(v) for k, v in (seen or {}).items()}
    state["baseline_complete"] = True
    save(state)


def touch_run(when=None) -> None:
    """Record that a check was attempted (updates last_run only, keeps the
    baseline). Called when the auto-check submits, so a run that fails or is
    cancelled doesn't re-fire on every dashboard load until one happens to
    succeed."""
    state = load()
    state["last_run"] = float(when) if when is not None else time.time()
    save(state)


def record_artist_seen(artist_id, album_ids) -> None:
    """Update one artist's baseline without touching last_run (that's the
    whole-library check's throttle). Used by the per-artist Artist-page scan so
    its new-release flagging shares the same baseline as the library check."""
    state = load()
    state.setdefault("seen", {})[str(artist_id)] = list(album_ids)
    save(state)
