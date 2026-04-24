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
    """Return ``{"last_run": float|None, "seen": {artist_id: [album_id, …]}}``,
    tolerating a missing or corrupt file with an empty baseline."""
    base = {"last_run": None, "seen": {}}
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
    """Persist the updated per-artist catalog snapshot and the run time."""
    save({"seen": seen, "last_run": when if when is not None else time.time()})
