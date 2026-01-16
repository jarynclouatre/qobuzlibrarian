"""Queue item construction.

"""


def _build_queue_item(*, album, album_dir, label, missing, present,
                      upgrade_only, auto_upgrade,
                      siblings_to_delete=None, quality=None,
                      force_track_by_track=False):
    """Bundle a confirmed download decision for batch processing.

    siblings_to_delete: list of sibling album dirs to remove
    after this item lands successfully. Carried inside the queue item so
    deletion can be deferred (album fill walk: queue persists across artists).

    force_track_by_track: when True the executor downloads each missing
    track individually and never fetches the whole-album URL, regardless
    of the missing/total ratio. Repair sets this so a tweak to the
    download_full_album heuristic can't silently turn a targeted
    truncation-repair into a wipe-and-replace of the whole album."""
    return {
        "album": album,
        "album_dir": album_dir,
        "label": label,
        "missing": missing,
        "present": present,
        "upgrade_only": upgrade_only,
        "auto_upgrade": auto_upgrade,
        "backup_path": None,
        "snapshot_before": None,
        "n_ok": 0,
        "n_fail": 0,
        "n_lossy": 0,
        "failed_tracks": [],
        "lossy_tracks": [],
        "elapsed": 0.0,
        "imported": False,
        "result": None,
        "siblings_to_delete": list(siblings_to_delete or []),
        "quality": quality,
        "force_track_by_track": bool(force_track_by_track),
    }
