"""Download-phase invariants for run_album_download — the strategy choice and
the n_ok/n_fail/n_lossy bookkeeping the single-album path and the queue executor
both lean on. The summary must keep n_ok + n_lossy + n_fail equal to the number
of tracks attempted, and a lossy fallback belongs in the lossy bucket only.
"""
import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian import download as dl


def _album(tracks):
    return {"id": "ALB", "title": "Album", "artist": {"name": "Artist"},
            "tracks": {"items": tracks}}


def test_match_key_from_stem_keys_a_star_track_to_its_title():
    """A lossy stem like "01. ★" must key to the same title as the Qobuz track,
    or the per-track retry can't match it back."""
    from qobuz_librarian.library.tags import normalize, strip_edition_suffix

    assert dl.match_key_from_stem("01. ★") == normalize(strip_edition_suffix("★"))
    assert dl.match_key_from_stem("03 - Changes") == normalize(
        strip_edition_suffix("Changes"))


def _patch(monkeypatch, *, rip, added, cleanup, cancel=False):
    monkeypatch.setattr(dl, "rip_url", rip)
    monkeypatch.setattr(dl, "files_added_since", added)
    monkeypatch.setattr(dl, "cleanup_lossy", cleanup)
    monkeypatch.setattr(dl, "snapshot_staging", lambda: set())
    monkeypatch.setattr(dl, "detect_auth_lost", lambda _o: False)
    monkeypatch.setattr(dl, "detect_disk_full", lambda _o: False)
    monkeypatch.setattr(dl, "detect_rate_limited", lambda _o: False)
    monkeypatch.setattr(dl, "is_cancel_requested", lambda: cancel)
    monkeypatch.setattr(dl.time, "sleep", lambda _s: None)


def test_full_album_with_present_tracks_counts_fail_against_total(monkeypatch, tmp_path):
    # Full-album rip re-downloads the WHOLE album (present tracks too), so n_fail
    # must be reconciled against n_tracks_total, not len(missing). Otherwise a
    # re-rip that drops a track reads as clean (n_fail=0) and the executor would
    # be cleared to delete a sibling / drop the gap-fill backup holding it.
    tracks = [{"id": i, "title": f"T{i}", "track_number": i} for i in range(1, 11)]
    missing = tracks[3:]          # 6 missing, 4 present → full-album strategy
    present = tracks[:4]
    album = tmp_path / "Artist" / "Album (2020)"
    album.mkdir(parents=True)
    # 8 of 10 land clean; 2 genuinely failed to re-rip.
    landed = [tmp_path / f"{i:02d} - T{i}.flac" for i in range(1, 9)]
    for p in landed:
        p.write_bytes(b"x")
    _patch(monkeypatch,
           rip=lambda *a, **k: (0, ""),
           added=lambda _s: landed,
           cleanup=lambda f: (list(f), [], []))
    monkeypatch.setattr(dl, "backup_gap_fill_files", lambda paths, d: None)
    monkeypatch.setattr(dl, "read_album_dir", lambda d: [])

    r = dl.run_album_download(album=_album(tracks), missing=missing,
                              present=present, album_dir=album, snapshot=set())
    assert r["download_full_album"] is True
    assert r["n_ok"] == 8
    assert r["n_fail"] == 2          # 10 total - 8 ok, NOT max(0, 6-8)=0
    assert r["n_ok"] + r["n_lossy"] + r["n_fail"] == 10


def test_lossy_track_retried_once_and_recovers(monkeypatch, tmp_path):
    tracks = [{"id": 1, "title": "A", "track_number": 1},
              {"id": 2, "title": "Star", "track_number": 2}]
    track_a = tmp_path / "01 - A.flac"
    track_a.write_bytes(b"x")
    star = tmp_path / "02 - Star.flac"
    rips = []

    def rip(url, **_k):
        rips.append(url)
        if "track/2" in url:
            star.write_bytes(b"x")        # the retry produces the missing FLAC
        return (0, "")

    deltas = iter([[track_a, tmp_path / "02 - Star.mp3"], [star]])
    cleans = iter([([track_a], ["02 - Star"], []), ([star], [], [])])
    _patch(monkeypatch, rip=rip,
           added=lambda _s: next(deltas, []),
           cleanup=lambda _f: next(cleans, ([], [], [])))
    monkeypatch.setattr(dl, "snapshot_staging", lambda: {track_a})

    r = dl.run_album_download(album=_album(tracks), missing=tracks, present=[],
                              album_dir=None, snapshot=set())

    # Exactly two rips: the album URL plus one per-track retry. A third would
    # mean the retry loops.
    assert rips == ["https://play.qobuz.com/album/ALB",
                    "https://play.qobuz.com/track/2"]
    assert (r["n_ok"], r["n_lossy"], r["n_fail"]) == (2, 0, 0)


@pytest.mark.parametrize("total,missing,expect_full", [
    (100, 69, False),   # 0.69 → per-track
    (100, 70, True),    # 0.70 → full-album
    (5, 3, False),      # below the max(4, …) floor → per-track
    (5, 4, True),       # hits the floor of 4 → full-album
])
def test_strategy_full_vs_per_track_boundary(monkeypatch, total, missing, expect_full):
    tracks = [{"id": i, "title": f"T{i}"} for i in range(total)]
    urls = []
    _patch(monkeypatch,
           rip=lambda url, **_k: (urls.append(url), (0, ""))[1],
           added=lambda _s: [],
           cleanup=lambda f: (list(f), [], []))

    dl.run_album_download(album=_album(tracks), missing=tracks[:missing],
                          present=[{}], album_dir=None, snapshot=set())

    assert any("/album/" in u for u in urls) is expect_full


def test_full_album_backs_up_present_tracks_before_rip(monkeypatch, tmp_path):
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    owned = album_dir / "01 - owned.flac"
    owned.write_bytes(b"the-owned-original")
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    tracks = [{"id": i, "title": f"T{i}"} for i in range(1, 6)]

    _patch(monkeypatch,
           rip=lambda *a, **k: (0, ""),
           added=lambda _s: [],
           cleanup=lambda f: (list(f), [], []))
    # existing=None drives the lazy read the queue executor relies on.
    monkeypatch.setattr(dl, "read_album_dir", lambda _d: [{"path": str(owned)}])
    monkeypatch.setattr(dl, "find_extras_in_existing", lambda *a, **k: [])

    result = {}
    dl.run_album_download(album=_album(tracks), missing=tracks[1:],
                          present=[tracks[0]], album_dir=album_dir, snapshot=set(),
                          result=result)

    bp = result["gap_fill_backup_path"]
    assert bp is not None and not owned.exists()
    assert any(f.read_bytes() == b"the-owned-original" for f in bp.rglob("*"))


def test_snapshot_staging_skips_the_beets_retry_tree(monkeypatch, tmp_path):
    """The retry-park tree (.beets_retry/) can hold hundreds of files from a
    long-running session, so snapshot_staging + files_added_since must skip it
    rather than walk it on every album (which would dominate per-album cost on
    big flushes); the rest of staging is still captured."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.integrations import rip

    staging = tmp_path / "staging"
    (staging / ".beets_retry" / "ParkedArt" / "ParkedAlbum").mkdir(parents=True)
    (staging / ".beets_retry" / "ParkedArt" / "ParkedAlbum" / "1.flac").write_bytes(b"x")
    (staging / "Artist" / "Album").mkdir(parents=True)
    (staging / "Artist" / "Album" / "1.flac").write_bytes(b"x")
    monkeypatch.setattr(cfg, "STAGING_DIR", staging)

    snap = rip.snapshot_staging()
    paths = {str(p.relative_to(staging)) for p in snap}
    assert paths == {"Artist/Album/1.flac"}     # the parked file is invisible

    # A new staging file is detected; one landing in the retry park is not
    # (it's not "this album's download").
    (staging / "Artist" / "Album" / "2.flac").write_bytes(b"y")
    (staging / ".beets_retry" / "ParkedArt" / "ParkedAlbum" / "2.flac").write_bytes(b"y")
    added = {str(p.relative_to(staging)) for p in rip.files_added_since(snap)}
    assert added == {"Artist/Album/2.flac"}
