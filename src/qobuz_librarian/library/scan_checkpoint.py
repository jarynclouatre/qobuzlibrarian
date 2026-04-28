"""Progress checkpoint for a resumable library scan.

A full-library scan can take a while; if it's interrupted (the container stops,
the box loses power) the work shouldn't be thrown away. As the scan finishes each
artist it records progress here — which artists are done, the albums found so far,
and the per-artist catalog snapshot for the new-release baseline. The next start
reads this and continues from where it left off rather than re-crawling.

The checkpoint is written only while a scan is mid-flight: a clean finish or a
deliberate cancel clears it, so its mere presence means "an unfinished scan is
waiting to resume."
"""
import json
import time

from qobuz_librarian import config as cfg


def load() -> dict | None:
    """The in-progress scan's checkpoint, or None if there's nothing to resume.

    Shape: ``{"kind": "missing"|"partial", "scanned": [folder_name, …],
    "candidates": [candidate_dict, …], "seen": {artist_id: [album_id, …]}}``.
    """
    try:
        data = json.loads(cfg.SCAN_CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") not in ("missing", "partial"):
        return None
    data.setdefault("scanned", [])
    data.setdefault("candidates", [])
    data.setdefault("seen", {})
    return data


def save(kind, scanned, candidates, seen) -> None:
    payload = {
        "kind": kind,
        "scanned": sorted(scanned),
        "candidates": candidates,
        "seen": seen,
        "ts": time.time(),
    }
    try:
        cfg.SCAN_CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.SCAN_CHECKPOINT_FILE.with_suffix(
            cfg.SCAN_CHECKPOINT_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(cfg.SCAN_CHECKPOINT_FILE)
    except OSError:
        pass


def clear() -> None:
    try:
        cfg.SCAN_CHECKPOINT_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def pending() -> dict | None:
    """A summary of an unfinished scan for the dashboard, or None. Returns
    ``{"kind", "done"}`` where done is how many artists are already scanned."""
    cp = load()
    if cp is None:
        return None
    return {"kind": cp["kind"], "done": len(cp["scanned"])}
