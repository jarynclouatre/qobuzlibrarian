"""Progress checkpoints for resumable library scans.

A full-library scan can take a while; if it's interrupted (the container stops,
the box loses power) the work shouldn't be thrown away. As the scan finishes each
artist it records progress here — which artists are done, the albums found so far,
and the per-artist catalog snapshot for the new-release baseline. The next start
reads this and continues from where it left off rather than re-crawling.

Progress is kept per scan **kind** ("missing" / "partial") in one file, so an
interrupted partial-fill scan isn't wiped when a missing-albums scan completes,
and vice-versa. A clean finish or a deliberate cancel clears that kind's entry;
a kind's presence means "an unfinished scan of that kind is waiting to resume."
"""
import json
import threading
import time

from qobuz_librarian import config as cfg

_KINDS = ("missing", "partial")

# save/clear are read-modify-write of the shared file; serialise them so two
# scan kinds progressing in parallel can't clobber each other's entry. Readers
# (load/pending) need no lock — _write swaps the file in atomically.
_lock = threading.Lock()


def _read() -> dict:
    try:
        data = json.loads(cfg.SCAN_CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data) -> None:
    try:
        cfg.SCAN_CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.SCAN_CHECKPOINT_FILE.with_suffix(
            cfg.SCAN_CHECKPOINT_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(cfg.SCAN_CHECKPOINT_FILE)
    except OSError:
        pass


def load(kind) -> dict | None:
    """This kind's checkpoint, or None. Shape: ``{"scanned": [folder_name, …],
    "candidates": [candidate_dict, …], "seen": {artist_id: [album_id, …]}}``."""
    cp = _read().get(kind)
    if not isinstance(cp, dict):
        return None
    cp.setdefault("scanned", [])
    cp.setdefault("candidates", [])
    cp.setdefault("seen", {})
    return cp


def save(kind, scanned, candidates, seen) -> None:
    with _lock:
        data = _read()
        data[kind] = {
            "scanned": sorted(scanned),
            "candidates": candidates,
            "seen": seen,
            "ts": time.time(),
        }
        _write(data)


def clear(kind) -> None:
    with _lock:
        data = _read()
        if kind not in data:
            return
        del data[kind]
        if data:
            _write(data)
            return
        try:
            cfg.SCAN_CHECKPOINT_FILE.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def pending() -> dict | None:
    """A summary of any unfinished scan for the dashboard, or None — missing
    takes precedence (it's the kind the first-run auto-scan runs). Returns
    ``{"kind", "done"}`` where done is how many artists are already scanned."""
    data = _read()
    for kind in _KINDS:
        cp = data.get(kind)
        if isinstance(cp, dict):
            return {"kind": kind, "done": len(cp.get("scanned", []))}
    return None
