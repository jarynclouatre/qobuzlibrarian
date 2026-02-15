"""Queue persistence — crash-safe save/load/clear and startup resume hook.

``save_pending_queue`` writes via tmp + ``os.replace`` so a crash mid-save
never leaves a half-written file. A version field is embedded in the
payload so a schema bump can be detected and old data discarded cleanly
on next launch.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.api.auth import AuthLost
from qobuz_librarian.queue.builder import _build_queue_item
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log, vlog


def _serialize_queue_item(item):
    """Pull the JSON-safe inputs out of an in-memory queue item."""
    return {
        "album": item["album"],
        "album_dir": str(item["album_dir"]) if item["album_dir"] else None,
        "label": item["label"],
        "missing": item["missing"],
        "present": item["present"],
        "upgrade_only": bool(item["upgrade_only"]),
        "auto_upgrade": bool(item["auto_upgrade"]),
        "siblings_to_delete": [str(p) for p in item.get("siblings_to_delete") or []],
        "quality": item.get("quality"),
        "force_track_by_track": bool(item.get("force_track_by_track", False)),
    }


def _deserialize_queue_item(d):
    """Rebuild a queue item from its on-disk form. Runtime fields
    (n_ok, backup_path, etc.) come back as fresh defaults from
    _build_queue_item, which is exactly what we want for replay."""
    return _build_queue_item(
        album=d["album"],
        album_dir=Path(d["album_dir"]) if d.get("album_dir") else None,
        label=d.get("label", ""),
        missing=d.get("missing") or [],
        present=d.get("present") or [],
        upgrade_only=bool(d.get("upgrade_only", False)),
        auto_upgrade=bool(d.get("auto_upgrade", False)),
        siblings_to_delete=[Path(p) for p in d.get("siblings_to_delete") or []],
        quality=d.get("quality"),
        force_track_by_track=bool(d.get("force_track_by_track", False)),
    )


def save_pending_queue(items, *, mode):
    """Atomically persist the current shared_queue to disk.

    Atomic = write to .tmp, then os.replace, so a crash mid-write can't
    leave a half-written file. Failures are logged but never raise —
    persistence is a safety net, not a hard requirement.
    """
    try:
        cfg.PENDING_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": cfg.PENDING_QUEUE_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "count": len(items),
            "items": [_serialize_queue_item(i) for i in items],
        }
        tmp = cfg.PENDING_QUEUE_FILE.with_suffix(cfg.PENDING_QUEUE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cfg.PENDING_QUEUE_FILE)
    except Exception as e:
        # Persistence failures shouldn't block the user from continuing
        # to walk — but they do mean a crash would cost decisions, so
        # surface them in --verbose.
        vlog(f"save_pending_queue failed: {e}")


def load_pending_queue():
    """Return (items, mode, saved_at_iso) if a pending file exists and
    parses, else (None, None, None). Schema-version mismatch returns
    None too with a user-visible warning."""
    if not cfg.PENDING_QUEUE_FILE.exists():
        return None, None, None
    try:
        payload = json.loads(cfg.PENDING_QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {cfg.PENDING_QUEUE_FILE.name} unreadable ({e}); ignoring."))
        return None, None, None
    if not isinstance(payload, dict):
        log.info(fmt(C.YELLOW,
            f"  ⚠  {cfg.PENDING_QUEUE_FILE.name} is malformed (not an object); "
            "ignoring."))
        return None, None, None
    ver = payload.get("version")
    if ver != cfg.PENDING_QUEUE_VERSION:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {cfg.PENDING_QUEUE_FILE.name} version {ver!r} not supported "
            f"(expected {cfg.PENDING_QUEUE_VERSION}); ignoring."))
        return None, None, None
    try:
        items = [_deserialize_queue_item(d) for d in payload.get("items") or []]
    except Exception as e:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {cfg.PENDING_QUEUE_FILE.name} contained malformed item "
            f"({e}); ignoring."))
        return None, None, None
    return items, payload.get("mode"), payload.get("saved_at")


def clear_pending_queue():
    """Remove the pending-queue file. Idempotent."""
    try:
        cfg.PENDING_QUEUE_FILE.unlink(missing_ok=True)
    except OSError as e:
        vlog(f"clear_pending_queue: {e}")


def offer_resume_pending_queue(args, token):
    """Startup hook. If a pending queue is present, prompt to resume / keep
    / discard. Returns True if main() should drop straight back to the
    menu without running anything else right now."""
    items, mode, saved_at = load_pending_queue()
    if not items:
        return False

    # Pretty-print the saved_at timestamp without dragging in tz juggling.
    when = saved_at or "unknown time"
    label_for_mode = {
        "album_walk": "Album fill walk",
        "walk_queue": "Walk + queue",
    }.get(mode or "", mode or "previous run")

    print()
    log.info(fmt(C.BOLD + C.YELLOW,
        f"  ⚠  Pending queue found: {len(items)} album(s) from {label_for_mode}"))
    log.info(fmt(C.GRAY, f"     Saved: {when}"))
    log.info(fmt(C.GRAY, f"     File:  {cfg.PENDING_QUEUE_FILE}"))
    log.info(fmt(C.GRAY,
        "     This means a previous run accumulated download decisions but "
        "didn't"))
    log.info(fmt(C.GRAY,
        "     finish flushing them. Resuming will download only what's "
        "in the queue."))
    print()

    while True:
        try:
            ans = input(fmt(C.CYAN,
                "  Resume now? [Y]es / [k]eep for later / [d]iscard: "
            )).strip().lower()
        except EOFError:
            ans = ""
        if ans in ("", "y", "yes"):
            log.info(fmt(C.CYAN,
                f"\n  ⟳  Resuming flush of {len(items)} album(s)…"))
            try:
                # Lazy import to avoid a circular dependency at module load:
                # executor imports several names from this module.
                from qobuz_librarian.queue.executor import _execute_download_queue
                _, beets_ok = _execute_download_queue(items, args, token)
                if beets_ok:
                    clear_pending_queue()
                    log.info(fmt(C.GREEN, "  ✓ Resume complete; pending file cleared."))
                else:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  beets import did not succeed — pending file "
                        f"kept for next launch ({len(items)} album(s) still queued)."))
            except KeyboardInterrupt:
                log.info(fmt(C.YELLOW,
                    "\n  ⚠  Resume interrupted; pending file kept for next launch."))
            except AuthLost:
                log.info(fmt(C.RED,
                    "  ✗ Auth lost during resume; pending file kept for next launch."))
                raise
            except Exception as e:
                log.info(fmt(C.YELLOW,
                    f"  ⚠  Resume failed: {e}. Pending file kept for next launch."))
            return False  # fall through to menu
        if ans in ("k", "keep"):
            log.info(fmt(C.GRAY, "  Keeping the pending queue. It'll prompt again next launch."))
            return False
        if ans in ("d", "discard"):
            try:
                conf = input(fmt(C.YELLOW,
                    f"  Really discard {len(items)} queued album(s)? "
                    "Type DISCARD to confirm: ")).strip()
            except EOFError:
                conf = ""
            if conf == "DISCARD":
                clear_pending_queue()
                log.info(fmt(C.GRAY, "  Pending queue cleared."))
            else:
                log.info(fmt(C.GRAY, "  Discard cancelled."))
            return False
        log.info(fmt(C.GRAY, "  Enter Y, k, or d."))
