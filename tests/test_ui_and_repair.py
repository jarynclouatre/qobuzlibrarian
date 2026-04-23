"""Tests for ui_cli prompts/menu, the repair log + scanner, and CLI entry
points. Most of the coverage is around the edge cases (truncated FLACs that
look healthy by duration, mid-write crashes, multi-URL paste handling).
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qobuz_librarian.modes.walk import (
    _album_seen_key,
    load_album_walk_seen,
    load_walk_seen,
    record_album_walk_seen,
    record_walk_seen,
)
from qobuz_librarian.repair_log import append_repair_log, scan_dir_for_isrc_repairs
from qobuz_librarian.ui_cli.menu import interactive_session_mode
from qobuz_librarian.ui_cli.prompts import (
    _read_fetch_log,
    confirm,
    interactive_query,
    log_fetch,
    parse_number_list,
    print_album_summary,
)

# ── Prompts: menu / interactive / confirm / number parsing ─────────────────

def test_interactive_session_mode_reprompts_on_garbage():
    with patch("builtins.input", side_effect=["xyz", "2"]):
        assert interactive_session_mode() == "artist"


def test_interactive_query_advertises_help_and_handles_cancel():
    # The prompt must advertise the '?' helper or no one finds it.
    fake = MagicMock(return_value="")
    with patch("builtins.input", fake):
        assert interactive_query() is None
    assert any("?=recent" in str(c.args) for c in fake.call_args_list)

    # '?' prints recent fetches and re-prompts.
    from qobuz_librarian.ui_cli import prompts as p
    with patch.object(p, "show_recent_fetches") as fake_show, \
         patch("builtins.input", side_effect=["?", ""]):
        assert p.interactive_query() is None
    assert fake_show.call_count == 1

    # 'q' at the Album sub-prompt cancels.
    with patch("builtins.input", side_effect=["Radiohead", "q"]):
        assert interactive_query() is None


def test_confirm_auto_yes_and_eof():
    assert confirm("Do it?", auto_yes=True) is True
    with patch("builtins.input", side_effect=EOFError):
        assert confirm("Do it?") is False


def test_parse_number_list_handles_gnarly_inputs():
    assert parse_number_list("3", 5) == [3]
    assert parse_number_list("2-4", 5) == [2, 3, 4]
    assert parse_number_list("all", 3) == [1, 2, 3]
    assert max(parse_number_list("3-10", 5)) <= 5
    # '²'.isdigit() is True but int('²') raises — must be ignored, not crash.
    assert parse_number_list("²", 10) == []
    assert parse_number_list("1-³", 10) == []


# ── Fetch log: malformed file robustness ─────────────────────────────────

def test_fetch_log_round_trip_and_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("qobuz_librarian.config.FETCH_LOG_FILE", tmp_path / "log.json")
    log_fetch({"ts": "2026-01-01", "artist": "Artist", "title": "Album"})
    assert _read_fetch_log()[0]["artist"] == "Artist"

    monkeypatch.setattr("qobuz_librarian.config.FETCH_LOG_FILE", tmp_path / "absent.json")
    assert _read_fetch_log() == []


def test_fetch_log_skips_malformed_or_non_dict_lines(tmp_path, monkeypatch):
    # A dashboard read must not blow up on garbage lines or hand-edited values
    # that JSON-parse but aren't dicts — those would crash entry.get('artist').
    f = tmp_path / "log.json"
    f.write_text('{"artist":"A"}\nNOT JSON\n42\n"oops"\n[1,2]\n{"artist":"B"}\n')
    monkeypatch.setattr("qobuz_librarian.config.FETCH_LOG_FILE", f)
    assert _read_fetch_log() == [{"artist": "A"}, {"artist": "B"}]

    # A legacy JSON array file with a failed migration must not be clobbered.
    from qobuz_librarian.ui_cli import prompts
    f2 = tmp_path / "legacy.json"
    f2.write_text(json.dumps([{"artist": "old"}]), encoding="utf-8")
    monkeypatch.setattr("qobuz_librarian.config.FETCH_LOG_FILE", f2)
    monkeypatch.setattr(prompts, "_migrate_fetch_log_to_jsonl", lambda: False)
    log_fetch({"artist": "new"})
    assert json.loads(f2.read_text(encoding="utf-8")) == [{"artist": "old"}]


# ── Repair log: concurrency + escaping ──────────────────────────────────

def test_append_repair_log_basics_and_escaping(tmp_path, monkeypatch):
    f = tmp_path / "repair.log"
    monkeypatch.setattr("qobuz_librarian.config.REPAIR_LOG_PATH", f)
    # First write succeeds with a header; second write doesn't add a second one.
    append_repair_log([{"artist": "A", "album": "B", "title": "T1"}])
    append_repair_log([{"artist": "A", "album": "B", "title": "T2"}])
    assert f.read_text().count("# Replaced-tracks log") == 1
    # Pipe in artist (AC|DC) must be escaped so log parse stays stable.
    append_repair_log([{"artist": "AC|DC", "album": "Back in Black", "title": "Hells Bells"}])
    assert "AC|DC" not in f.read_text().split("\n")[-2]


def test_append_repair_log_concurrent_appends_stay_parseable(tmp_path, monkeypatch):
    # 40 simultaneous appends from a thread pool must all land, each on its
    # own line, and the header must still appear exactly once.
    from concurrent.futures import ThreadPoolExecutor
    f = tmp_path / "repair.log"
    monkeypatch.setattr("qobuz_librarian.config.REPAIR_LOG_PATH", f)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(append_repair_log,
                               [{"artist": f"Artist{i}", "album": "B", "title": f"Title{i}"}])
                   for i in range(40)]
        assert all(fut.result(timeout=5) for fut in futures)
    text = f.read_text()
    assert text.count("# Replaced-tracks log") == 1
    titles = {ln.rsplit("|", 1)[-1].strip()
              for ln in text.splitlines() if ln and not ln.startswith("#")}
    assert titles == {f"Title{i}" for i in range(40)}


# ── scan_dir_for_isrc_repairs: the truncation gates ────────────────────

def _track(isrc="GB1234567890", length=240.0, path="/music/track.flac", **kw):
    return {"isrc": isrc, "length": length, "title": "Track", "path": path,
            "sample_rate": 44100, "bits": 16, "channels": 2, "tracknumber": 1, **kw}


def test_scan_isrc_repairs_truncation_gates(tmp_path):
    # Both gates (duration mismatch + decode) must fire for a "verified truncated".
    track = _track(length=169.0)
    qt = {"duration": 200.0, "title": "T", "track_number": 1}
    with patch("qobuz_librarian.repair_log.read_album_dir", return_value=[track]), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc", return_value=qt):
        assert len(scan_dir_for_isrc_repairs(tmp_path, "token")["verified_truncated"]) == 1

    # Zero Qobuz duration → no reliable comparison → don't flag healthy files.
    with patch("qobuz_librarian.repair_log.read_album_dir", return_value=[_track(length=10.0)]), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value={"duration": 0, "title": "T", "track_number": 1}):
        assert scan_dir_for_isrc_repairs(tmp_path, "token")["verified_ok"] == 1

    # No Qobuz duration BUT decode probe fails → flag corruption.
    bad = _track(length=0.0, path=str(tmp_path / "x.flac"))
    with patch("qobuz_librarian.repair_log.read_album_dir", return_value=[bad]), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               return_value={"duration": 0, "title": "T", "track_number": 1}), \
         patch("qobuz_librarian.repair_log._flac_decode_ok", return_value=False):
        assert len(scan_dir_for_isrc_repairs(tmp_path, "token")["verified_truncated"]) == 1


def test_scan_isrc_repairs_records_no_isrc_with_file_size(tmp_path):
    flac = tmp_path / "tiny.flac"
    flac.write_bytes(b"\x00" * 5_000)
    track = _track(isrc="", length=0.0, path=str(flac), sample_rate=0, bits=0)
    with patch("qobuz_librarian.repair_log.read_album_dir", return_value=[track]):
        result = scan_dir_for_isrc_repairs(tmp_path, "token")
    entry = result["no_isrc_tag"][0]
    assert entry["size_bytes"] == 5_000 and "likely-corrupted" in entry["diagnostic"]


def test_scan_isrc_repairs_byte_size_short_catches_tail_truncation(tmp_path):
    # mutagen reads `length` from STREAMINFO which survives tail truncation —
    # so a tail-truncated FLAC reports the original duration. The byte-size
    # gate catches it: 5 kB can't hold 200 s of 44.1k/16-bit/stereo audio.
    flac = tmp_path / "tail_truncated.flac"
    flac.write_bytes(b"\x00" * 5_000)
    track = _track(length=200.0, path=str(flac))
    qt = {"duration": 200.0, "title": "T", "track_number": 1}
    with patch("qobuz_librarian.repair_log.read_album_dir", return_value=[track]), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc", return_value=qt):
        result = scan_dir_for_isrc_repairs(tmp_path, "token")
    assert result["verified_truncated"][0]["reason"] == "byte_size_short"


def test_scan_isrc_repairs_sweep_mode_skips_qobuz_for_healthy_files(tmp_path):
    # deep=False: a healthy file passes with no Qobuz call (the speed win),
    # but a clearly-truncated one is still flagged without one.
    healthy = tmp_path / "ok.flac"
    healthy.write_bytes(b"\x00" * 2_000_000)
    calls = []
    with patch("qobuz_librarian.repair_log.read_album_dir",
               return_value=[_track(length=10.0, path=str(healthy))]), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc",
               side_effect=lambda *a, **k: calls.append(a)):
        r = scan_dir_for_isrc_repairs(tmp_path, "token", deep=False)
    assert r["verified_ok"] == 1 and calls == []

    short = tmp_path / "short.flac"
    short.write_bytes(b"\x00" * 5_000)
    qt = {"duration": 200.0, "title": "T", "track_number": 1}
    with patch("qobuz_librarian.repair_log.read_album_dir",
               return_value=[_track(length=200.0, path=str(short))]), \
         patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc", return_value=qt):
        r = scan_dir_for_isrc_repairs(tmp_path, "token", deep=False)
    assert len(r["verified_truncated"]) == 1


def test_flac_metadata_size_walks_blocks_and_bails_on_non_flac(tmp_path):
    # The byte-size gate trims metadata off before comparing, so the helper
    # must walk the block chain on a real FLAC and bail on anything else.
    from qobuz_librarian.repair_log import _flac_metadata_size

    real = tmp_path / "ok.flac"
    real.write_bytes(
        b"fLaC"
        + b"\x00\x00\x00\x22" + b"\x00" * 34   # STREAMINFO, not last
        + b"\x84\x00\x00\x0a" + b"\x00" * 10   # VORBIS_COMMENT, last-bit on
        + b"frame data"
    )
    assert _flac_metadata_size(str(real)) == 4 + (4 + 34) + (4 + 10)

    fake = tmp_path / "no.mp3"
    fake.write_bytes(b"ID3\x04\x00" + b"\x00" * 50)
    assert _flac_metadata_size(str(fake)) == 0


# ── Real-FLAC integration: catches both decode-probe and silence cases ─

@pytest.fixture
def _need_ffmpeg():
    import shutil
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")


def test_scan_dir_real_flac_decode_probe_catches_tail_truncation(tmp_path, _need_ffmpeg):
    import subprocess

    from mutagen.flac import FLAC
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    flac = album / "01.flac"
    # White noise compresses poorly, so the file stays above the byte-size
    # threshold even after a small tail truncation — forcing the decode
    # probe to be the actual signal.
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anoisesrc=duration=10:color=white:sample_rate=44100:amplitude=0.3",
         "-ac", "2", "-c:a", "flac", str(flac)], check=True)
    f = FLAC(str(flac))
    f["isrc"], f["title"], f["tracknumber"] = ["US1234500002"], ["Tail Truncated"], ["1"]
    f.save()
    assert flac.stat().st_size > 500_000
    flac.write_bytes(flac.read_bytes()[:-10_240])

    qt = {"duration": 10.0, "title": "Tail Truncated", "track_number": 1}
    with patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc", return_value=qt):
        r = scan_dir_for_isrc_repairs(album, "token")
    assert r["verified_truncated"][0]["reason"] == "decode_failed"


def test_scan_dir_real_flac_quiet_silence_not_flagged(tmp_path, _need_ffmpeg):
    # Silence compresses below the byte-size gate, but the file is healthy.
    # The decode corroboration must keep it from being flagged.
    import subprocess

    from mutagen.flac import FLAC
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    flac = album / "01.flac"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=stereo", "-t", "30", "-c:a", "flac", str(flac)],
        check=True)
    f = FLAC(str(flac))
    f["isrc"], f["title"], f["tracknumber"] = ["US1234500003"], ["Silent"], ["1"]
    f.save()
    assert flac.stat().st_size < 100_000

    qt = {"duration": 30.0, "title": "Silent", "track_number": 1}
    with patch("qobuz_librarian.repair_log.find_qobuz_track_by_isrc", return_value=qt):
        r = scan_dir_for_isrc_repairs(album, "token")
    assert r["verified_truncated"] == [] and r["verified_ok"] == 1


# ── Web /repair: no-ISRC recovery flow ──────────────────────────────────

def _no_isrc_scan_result(album_dir):
    return {"verified_truncated": [], "verified_ok": 0,
            "no_isrc_tag": [{"path": str(album_dir / "01.flac"),
                              "title": "Bad Track", "size_bytes": 5_000,
                              "diagnostic": "likely-corrupted (5,000 B)"}],
            "isrc_no_match": []}


def _run_no_isrc_recovery(tmp_path, caplog, matched):
    from qobuz_librarian.web import flows
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    job = MagicMock()
    job.cancel_requested = False
    with patch.object(flows, "list_library_artists", return_value=[album_dir.parent]), \
         patch.object(flows, "list_artist_album_dirs", return_value=[album_dir]), \
         patch.object(flows, "clear_scan_caches"), \
         patch.object(flows, "find_qobuz_album_for_dir", return_value=matched), \
         patch("qobuz_librarian.repair_log.scan_dir_for_isrc_repairs",
               return_value=_no_isrc_scan_result(album_dir)):
        with caplog.at_level("INFO", logger="qobuz_librarian"):
            flows.scan_repairs(job, "token")
    return job, " ".join(r.getMessage() for r in caplog.records)


def test_no_isrc_recovery_offers_whole_album_redownload_when_matched(tmp_path, caplog):
    matched = {"id": "alb123", "title": "Real Album", "release_date_original": "2013-01-01"}
    job, _ = _run_no_isrc_recovery(tmp_path, caplog, matched)
    assert job.add_candidate.called
    kw = job.add_candidate.call_args.kwargs
    assert kw["kind"] == "redownload" and kw["payload"]["album_id"] == "alb123"
    assert "Real Album" in kw["detail"]


def test_no_isrc_recovery_falls_back_to_hand_verify_when_unmatched(tmp_path, caplog):
    job, messages = _run_no_isrc_recovery(tmp_path, caplog, None)
    assert not job.add_candidate.called
    assert "Bad Track" in messages and "check by hand" in messages


def test_no_isrc_redownload_failure_restores_original_folder(tmp_path, monkeypatch):
    from qobuz_librarian.web import flows
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    backup_dir = tmp_path / "backup"
    restored = {}
    monkeypatch.setattr(flows, "get_album", lambda *a: {"id": "x"})
    monkeypatch.setattr("qobuz_librarian.library.backup.backup_album_dir", lambda d: backup_dir)
    monkeypatch.setattr("qobuz_librarian.modes.process.process_album",
                        lambda *a, **k: {"imported": False, "n_ok": 0})
    monkeypatch.setattr("qobuz_librarian.library.backup.restore_upgrade_backup",
                        lambda bp, d: restored.update(bp=bp, dir=d) or True)
    res = flows._redownload_damaged_album(
        {"album_dir": str(album_dir), "album_id": "x"}, "token")
    assert res["n_ok"] == 0
    assert restored == {"bp": backup_dir, "dir": album_dir}


# ── Repair: relocate refilled tracks back to the album folder ─────────

def test_repair_relocates_refilled_track_back_to_album_dir(tmp_path, monkeypatch):
    from qobuz_librarian.modes import repair
    album_dir = tmp_path / "First Fires (2013)"
    landed_dir = tmp_path / "The North Borders (2013)"
    album_dir.mkdir()
    landed_dir.mkdir()
    refill = landed_dir / "01 - First Fires.flac"
    refill.write_bytes(b"flac-bytes")
    (landed_dir / "cover.jpg").write_bytes(b"art")

    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: ([{"path": str(refill), "isrc": "GBCFB1300101"}]
                                    if d == landed_dir and refill.exists() else []))
    monkeypatch.setattr(repair, "_sync_beets_db_after_file_move", lambda *a: None)
    moved = repair._relocate_refilled_into_album_dir(
        album_dir, landed_dir, {"GBCFB1300101"}, before_paths=set(), landed_was_new=True)
    assert moved == 1
    assert (album_dir / "01 - First Fires.flac").exists() and not refill.exists()
    assert not landed_dir.exists()  # invented folder removed wholesale


def test_repair_leaves_a_preexisting_track_sharing_the_recording_alone(tmp_path, monkeypatch):
    # A track that was already in the target dir's sibling album under the
    # same ISRC must NOT be moved — it isn't a refill, it's an existing copy.
    from qobuz_librarian.modes import repair
    album_dir = tmp_path / "First Fires (2013)"
    owned_dir = tmp_path / "The North Borders (2013)"
    album_dir.mkdir()
    owned_dir.mkdir()
    owned = owned_dir / "01 - First Fires.flac"
    owned.write_bytes(b"already-here")

    monkeypatch.setattr(repair, "read_album_dir",
                        lambda d: ([{"path": str(owned), "isrc": "GBCFB1300101"}]
                                    if d == owned_dir else []))
    monkeypatch.setattr(repair, "_sync_beets_db_after_file_move", lambda *a: None)
    moved = repair._relocate_refilled_into_album_dir(
        album_dir, owned_dir, {"GBCFB1300101"},
        before_paths={str(owned)}, landed_was_new=False)
    assert moved == 0 and owned.exists()
    assert not (album_dir / "01 - First Fires.flac").exists()


# ── CLI parse_args guards ───────────────────────────────────────────────

def _parse_argv(argv):
    import sys

    from qobuz_librarian.cli import parse_args
    with patch.object(sys, "argv", ["qobuz-librarian", *argv]):
        return parse_args()


def test_parse_args_rejects_incompatible_flag_combos():
    # Each of these combos silently dropped one side before — reject at parse.
    invalid = [
        ["--auto-safe", "Some Artist - Album"],
        ["--force", "--artist", "Radiohead"],
        ["--artist", ""],
        ["--artist", "   "],
        ["--no-catalog", "Some Artist - Album"],
        ["--include-comps", "--upgrade-walk"],
        ["--no-upgrade", "--upgrade-walk"],
        ["--include-singles", "--upgrade-walk"],
        ["--artist", "Radiohead", "--upgrade-walk"],
        ["--artist", "Four Tet", "some album"],
        ["--reset-walk-seen", "--artist", "Radiohead"],
        ["--reset-walk-seen", "Some Artist - Album"],
    ]
    for argv in invalid:
        with pytest.raises(SystemExit):
            _parse_argv(argv)


def test_parse_args_exit_code_is_one_not_two():
    # argparse defaults to 2; we use 1 so CI tooling distinguishes our user-
    # facing rejections from interpreter crashes.
    for argv in [["--force", "--artist", "Radiohead"], ["--no-such-flag"]]:
        with pytest.raises(SystemExit) as exc:
            _parse_argv(argv)
        assert exc.value.code == 1


# ── CLI plumbing: settings load + logging + quiet + die ────────────────

def test_cli_main_loads_persisted_settings_before_parsing(monkeypatch):
    import sys

    from qobuz_librarian import cli
    from qobuz_librarian.web import settings_store
    load_count = [0]
    monkeypatch.setattr(settings_store, "load",
                        lambda: load_count.__setitem__(0, load_count[0] + 1))
    monkeypatch.setattr(cli, "parse_args", lambda: sys.exit(0))
    with pytest.raises(SystemExit):
        cli.main()
    assert load_count[0] == 1


def test_file_logging_strips_ansi_and_persists(tmp_path):
    from qobuz_librarian.ui_cli import logging as qlog
    log_path = tmp_path / "qobuz-librarian.log"
    if qlog._file_handler is not None:
        qlog.log.removeHandler(qlog._file_handler)
        qlog._file_handler = None
    qlog.attach_file_handler(log_path, "INFO")
    try:
        qlog.log.info("\x1b[31mred message\x1b[0m")
        qlog._file_handler.flush()
        contents = log_path.read_text()
        assert "red message" in contents and "\x1b" not in contents
    finally:
        qlog.log.removeHandler(qlog._file_handler)
        qlog._file_handler = None


def test_quiet_flag_raises_logger_above_info():
    import logging

    from qobuz_librarian.ui_cli.logging import log, set_quiet
    set_quiet(True)
    try:
        assert log.level == logging.WARNING
    finally:
        set_quiet(False)
        assert log.level == logging.INFO


def test_die_uses_provided_exit_code(capsys):
    from qobuz_librarian.ui_cli.errors import EXIT_AUTH, die
    with pytest.raises(SystemExit) as exc:
        die("auth failure msg", EXIT_AUTH)
    assert exc.value.code == EXIT_AUTH
    assert "auth failure msg" in capsys.readouterr().err


def test_parse_qobuz_url():
    from qobuz_librarian.cli import parse_qobuz_url
    assert parse_qobuz_url("https://play.qobuz.com/album/abc12345") == ("album", "abc12345")
    assert parse_qobuz_url("https://example.com/something") is None


# ── interactive_query: non-Qobuz URL handling ───────────────────────────

def test_interactive_query_warns_on_non_qobuz_url(caplog):
    import logging

    from qobuz_librarian.ui_cli.prompts import interactive_query
    with patch("builtins.input",
               side_effect=["https://example.com/album/123", "q"]):
        with caplog.at_level(logging.INFO, logger="qobuz_librarian"):
            result = interactive_query()
    assert result is None
    assert any("Only Qobuz URLs" in r.message for r in caplog.records)


def test_album_mode_track_url_at_prompt_explains_clearly(capsys):
    import types

    from qobuz_librarian.modes.album import run_album_mode
    args = types.SimpleNamespace(
        query=[], dry_run=False, force=False, yes=False, no_import=False,
        verbose=False, consolidate=False, no_upgrade=False, prefer_hires=False,
        no_compress=False, include_singles=False, auto_safe=False, upgrade_walk=False)
    with patch("qobuz_librarian.modes.album.interactive_query",
               return_value=("__url__", "https://play.qobuz.com/track/12345")), \
         patch("qobuz_librarian.modes.album.clear_scan_caches"):
        with pytest.raises(SystemExit):
            run_album_mode(args, "tok", loop=False)
    assert "track URL" in capsys.readouterr().err


def test_album_mode_aborted_at_query_prompt_breaks_loop():
    import types

    from qobuz_librarian.api.auth import Aborted
    from qobuz_librarian.modes.album import run_album_mode
    album = {"id": "A1", "title": "Album", "artist": {"name": "Artist"}}
    args = types.SimpleNamespace(
        query=["abbey", "road"], dry_run=False, force=False, yes=False,
        no_import=False, verbose=False, consolidate=False, no_upgrade=False,
        prefer_hires=False, no_compress=False, include_singles=False,
        auto_safe=False, upgrade_walk=False)
    calls = []

    def fake_resolve(a, tok):
        calls.append(1)
        if len(calls) == 1:
            return album
        raise Aborted("loop exit")

    with patch("qobuz_librarian.modes.album.resolve_album_from_args",
               side_effect=fake_resolve), \
         patch("qobuz_librarian.modes.album._interactive_album_action"), \
         patch("qobuz_librarian.modes.album.process_album") as mock_process:
        run_album_mode(args, "tok", loop=True)
    assert mock_process.called is False


# ── Repair: backup resolution branches ─────────────────────────────────

def _call_repair_album_dir(tmp_path, monkeypatch, *, n_ok, n_fail, imported):
    from argparse import Namespace

    import qobuz_librarian.modes.repair as repair_mod
    album_dir = tmp_path / "Artist" / "Album (2020)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - Track.flac"
    track.write_bytes(b"\x00" * 200)
    monkeypatch.setattr("qobuz_librarian.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("qobuz_librarian.config.REPAIR_LOG_PATH", tmp_path / "repair.log")
    monkeypatch.setattr(repair_mod, "get_album",
                        lambda aid, tok: {"id": aid, "title": "Album", "tracks": {"items": []}})

    def fake_execute(queue, args, token):
        for qi in queue:
            qi["n_ok"] = n_ok
            qi["n_fail"] = n_fail
            qi["imported"] = imported

    monkeypatch.setattr(repair_mod, "_execute_download_queue", fake_execute)
    monkeypatch.setattr(repair_mod, "append_repair_log", lambda e: True)

    vt = [{"path": str(track), "title": "Track 01",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0}]
    args = Namespace(force=False, yes=True, prefer_hires=False, consolidate=False, no_upgrade=False)
    return repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok"), tmp_path


def _backup_files(tmp_path):
    root = tmp_path / "backups"
    return list(root.rglob("*")) if root.exists() else []


def test_repair_backup_dropped_on_full_success_and_restored_on_silent_beets_failure(tmp_path, monkeypatch):
    # Full success: backup is consumed (dropped).
    result, p = _call_repair_album_dir(tmp_path / "ok", monkeypatch,
                                       n_ok=1, n_fail=0, imported=True)
    assert [f for f in _backup_files(p) if f.is_file()] == []
    assert result["n_ok"] == 1

    # Silent beets failure (downloads succeeded but import didn't): roll back
    # to the pre-repair originals from the backup.
    result, p = _call_repair_album_dir(tmp_path / "silent", monkeypatch,
                                       n_ok=1, n_fail=0, imported=False)
    assert [f for f in _backup_files(p) if f.is_file()] == []
    assert (p / "Artist" / "Album (2020)" / "01 - Track.flac").exists()
    assert result["imported"] is False


def test_repair_backup_kept_when_downloads_fail_and_skipped_when_backup_fails(tmp_path, monkeypatch):
    # Downloads fail → backup is preserved for manual recovery.
    _call_repair_album_dir(tmp_path / "kept", monkeypatch, n_ok=0, n_fail=1, imported=False)
    assert _backup_files(tmp_path / "kept")

    # Backup itself fails → original must NOT be queued for replacement.
    from argparse import Namespace

    import qobuz_librarian.modes.repair as repair_mod
    album_dir = tmp_path / "nb" / "Artist" / "Album (2020)"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 - Track.flac"
    track.write_bytes(b"\x00" * 200)
    monkeypatch.setattr(repair_mod, "backup_gap_fill_files", lambda paths, d: None)
    monkeypatch.setattr(repair_mod, "_execute_download_queue",
                        lambda *a: (_ for _ in ()).throw(
                            AssertionError("must not run when backup fails")))
    vt = [{"path": str(track), "title": "Track 01",
           "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
           "file_length": 5.0}]
    args = Namespace(force=False, yes=True, prefer_hires=False, consolidate=False, no_upgrade=False)
    res = repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok")
    assert track.exists() and res["n_fail"] == len(vt)


# ── Walk-seen / album-walk-seen state ───────────────────────────────────

def test_walk_seen_records_idempotently_and_survives_a_crashed_rename(tmp_path, monkeypatch):
    import qobuz_librarian.modes.walk as walk_mod
    f = tmp_path / "walk_seen.txt"
    monkeypatch.setattr("qobuz_librarian.config.WALK_SEEN_FILE", f)
    record_walk_seen("Radiohead")
    record_walk_seen("Radiohead")  # idempotent
    assert "radiohead" in load_walk_seen()
    prior = f.read_bytes()

    # If os.replace fails the file must not be half-written.
    monkeypatch.setattr(walk_mod.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError("crashed")))
    record_walk_seen("Portishead")
    assert f.read_bytes() == prior
    assert load_walk_seen() == {"radiohead"}


def test_album_walk_seen_normalises_and_dedupes(tmp_path, monkeypatch):
    f = tmp_path / "album_walk_seen.txt"
    monkeypatch.setattr("qobuz_librarian.config.ALBUM_WALK_SEEN_FILE", f)
    # The key folds slashes and whitespace so 'AC/DC' and 'ACDC' don't split.
    assert _album_seen_key("AC/DC", "Highway to Hell") == "acdc::highwaytohell"
    record_album_walk_seen("Radiohead", "OK Computer")
    record_album_walk_seen("Radiohead", "OK Computer")  # idempotent
    lines = [l for l in f.read_text().splitlines() if " | " in l and not l.startswith("#")]
    assert len(lines) == 1
    assert _album_seen_key("Radiohead", "OK Computer") in load_album_walk_seen()


def test_album_walk_filter_is_a_substring_match(monkeypatch):
    # Filter 'a' must hit every artist containing 'a', not just those starting
    # with it. Otherwise the user can't surface mid-name letters.
    from types import SimpleNamespace

    from qobuz_librarian.modes import walk

    def _fake_artist(name):
        p = MagicMock(spec=Path)
        p.name = name
        return p

    monkeypatch.setattr(walk, "list_library_artists",
                        lambda: [_fake_artist(n) for n in
                                 ["Beatles", "David Bowie", "Albert Collins", "Radiohead"]])
    monkeypatch.setattr(walk, "load_album_walk_seen", lambda: set())
    seen = []
    monkeypatch.setattr(walk, "run_artist_gap_fill",
                        lambda artist_query, *_a, **_k: (seen.append(artist_query) or
                                                         ([], set(), set(), None, None)))
    monkeypatch.setattr(walk, "list_artist_album_dirs", lambda d: [])
    monkeypatch.setattr(walk, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(walk, "save_pending_queue", lambda *a, **k: None)
    monkeypatch.setattr(walk, "confirm", lambda *a, **k: False)

    args = SimpleNamespace(consolidate=False, yes=False, dry_run=False)
    with patch("builtins.input", side_effect=["a"]):
        walk.run_album_walk_mode(args, "tok")
    assert sorted(seen) == ["Albert Collins", "Beatles", "David Bowie", "Radiohead"]


def test_album_walk_stop_halts_the_walk_without_burying_the_album(tmp_path, monkeypatch):
    # 's' at an album prompt means "I'm done for now", not "dismiss this album".
    # It must stop the whole walk and leave the album on screen un-recorded, so
    # the next walk re-offers it.
    from types import SimpleNamespace

    from qobuz_librarian.modes import walk

    f = tmp_path / "album_walk_seen.txt"
    monkeypatch.setattr("qobuz_librarian.config.ALBUM_WALK_SEEN_FILE", f)

    def _fake_artist(name):
        p = MagicMock(spec=Path)
        p.name = name
        return p

    monkeypatch.setattr(walk, "list_library_artists",
                        lambda: [_fake_artist(n) for n in ["Abba", "Beatles", "Cream"]])
    monkeypatch.setattr(walk, "list_artist_album_dirs", lambda d: [])
    monkeypatch.setattr(walk, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(walk, "save_pending_queue", lambda *a, **k: None)

    scanned = []

    def _fake_gap_fill(artist_query, *_a, **_k):
        scanned.append(artist_query)
        stopped_dir = SimpleNamespace(name="Arrival")
        return [{"dir": stopped_dir, "result": "user_stopped"}], {}, set(), set(), None, []

    monkeypatch.setattr(walk, "run_artist_gap_fill", _fake_gap_fill)

    args = SimpleNamespace(consolidate=False, yes=False, dry_run=False)
    with patch("builtins.input", side_effect=[""]):
        walk.run_album_walk_mode(args, "tok")

    assert scanned == ["Abba"]            # stopped before reaching Beatles/Cream
    assert load_album_walk_seen() == set()  # the stopped-on album was not buried


# ── Scan-report-repair classifications ──────────────────────────────────

def _call_scan_report(tmp_path, monkeypatch, *, repair_result=None,
                     verified_truncated=None, yes=True, input_return="y"):
    from argparse import Namespace

    import qobuz_librarian.modes.repair as repair_mod
    from qobuz_librarian.modes.repair import _scan_report_repair
    album_dir = tmp_path / "Artist" / "Album (2022)"
    album_dir.mkdir(parents=True)
    (album_dir / "01 Track.flac").write_bytes(b"\x00" * 200)
    if verified_truncated is None:
        verified_truncated = [{"path": str(album_dir / "01 Track.flac"),
                                "title": "Track 01", "isrc": "USRC12345678",
                                "track_number": 1, "file_length": 5.0,
                                "qobuz_duration": 180.0,
                                "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "A1"}}}]
    monkeypatch.setattr(repair_mod, "scan_dir_for_isrc_repairs",
                        lambda *a, **k: {"verified_truncated": verified_truncated,
                                         "verified_ok": 0, "isrc_no_match": [], "no_isrc_tag": []})
    if repair_result is not None:
        monkeypatch.setattr(repair_mod, "repair_album_dir", lambda *a, **k: repair_result)
    monkeypatch.setattr(repair_mod, "section", lambda *a: None)
    args = Namespace(force=False, yes=yes, prefer_hires=False, consolidate=False, no_upgrade=False)
    with patch("builtins.input", return_value=input_return):
        return _scan_report_repair(album_dir, "Artist", args, "tok")


def test_scan_report_classifies_repair_outcomes(tmp_path, monkeypatch):
    # Repair succeeds → "repaired".
    assert _call_scan_report(tmp_path / "ok", monkeypatch,
                             repair_result={"n_ok": 1, "n_fail": 0, "imported": True, "backup": None}) == "repaired"
    # Downloads succeeded but beets failed silently → classified as failure.
    assert _call_scan_report(tmp_path / "silent", monkeypatch,
                             repair_result={"n_ok": 1, "n_fail": 0, "imported": False, "backup": None}) == "failed"
    # Nothing truncated → "clean".
    assert _call_scan_report(tmp_path / "clean", monkeypatch, verified_truncated=[]) == "clean"
    # User declines the prompt → "skipped".
    assert _call_scan_report(tmp_path / "skip", monkeypatch,
                             yes=False, input_return="n") == "skipped"


# ── Mode entry points: clean returns on misses/cancels ─────────────────

def test_mode_entry_points_return_cleanly_on_misses_and_empty_libraries():
    import types

    from qobuz_librarian.api.auth import CatalogMiss

    args = types.SimpleNamespace(
        query="x", dry_run=False, force=False, yes=True, no_import=False,
        verbose=False, consolidate=False, no_upgrade=False, prefer_hires=False,
        no_compress=False, include_singles=False, auto_safe=False, upgrade_walk=False)

    # Album mode: catalog miss returns None (not exception).
    from qobuz_librarian.modes.album import run_album_mode
    with patch("qobuz_librarian.modes.album.resolve_album_from_args",
               side_effect=CatalogMiss("not found")), \
         patch("qobuz_librarian.modes.album.clear_scan_caches"):
        assert run_album_mode(args, "tok", loop=False) is None

    # Repair-album: user cancels picker → clean return.
    from qobuz_librarian.modes.repair import run_album_repair_mode
    rargs = types.SimpleNamespace(query=None, dry_run=False, force=False, yes=False,
                                   no_import=False, verbose=False, consolidate=False,
                                   no_upgrade=False, prefer_hires=False, no_compress=False)
    with patch("qobuz_librarian.modes.repair._prompt_library_album_for_repair",
               return_value=(None, None)), \
         patch("qobuz_librarian.modes.repair.clear_scan_caches"):
        assert run_album_repair_mode(rargs, "tok", loop=False) is None

    # Upgrade walk: empty library → clean return.
    from qobuz_librarian.modes.upgrade import run_upgrade_walk_mode
    uargs = types.SimpleNamespace(dry_run=False, yes=False, auto_safe=False, force=False,
                                   consolidate=False, no_import=False, verbose=False,
                                   no_compress=False, prefer_hires=False)
    with patch("qobuz_librarian.modes.upgrade.list_library_artists", return_value=[]), \
         patch("qobuz_librarian.modes.upgrade.clear_scan_caches"):
        assert run_upgrade_walk_mode(uargs, "tok") is None


def test_album_mode_forwards_resolved_album_and_handles_auth_lost():
    import types

    from qobuz_librarian.api.auth import AuthLost
    from qobuz_librarian.modes.album import run_album_mode
    from qobuz_librarian.ui_cli.errors import EXIT_AUTH

    args = types.SimpleNamespace(
        query="abbey road", dry_run=False, force=False, yes=True, no_import=False,
        verbose=False, consolidate=False, no_upgrade=False, prefer_hires=False,
        no_compress=False, include_singles=False, auto_safe=False, upgrade_walk=False)
    album = {"id": "A1", "title": "Abbey Road", "artist": {"name": "The Beatles"}}

    with patch("qobuz_librarian.modes.album.resolve_album_from_args",
               return_value=album), \
         patch("qobuz_librarian.modes.album.process_album") as mock_process, \
         patch("qobuz_librarian.modes.album.clear_scan_caches"):
        run_album_mode(args, "tok", loop=False)
    assert mock_process.call_args[0][0] is album

    # AuthLost surfaces the auth exit code, not a crash.
    with patch("qobuz_librarian.modes.album.resolve_album_from_args",
               side_effect=AuthLost("401 from test")), \
         patch("qobuz_librarian.modes.album.clear_scan_caches"):
        with pytest.raises(SystemExit) as exc:
            run_album_mode(args, "tok", query_args=["x"], loop=False)
    assert exc.value.code == EXIT_AUTH


def test_print_album_summary_renders_null_fields_as_placeholders(caplog):
    # A Qobuz album that came back with null title/artist must not log "None".
    import logging
    null_album = {"id": "1", "title": None, "artist": None, "tracks_count": 10,
                  "maximum_bit_depth": 16, "maximum_sampling_rate": 44.1,
                  "released_at": 0}
    with caplog.at_level(logging.INFO):
        print_album_summary(null_album, missing=[], present=[{"id": "t1"}],
                            album_dir=None, force=False)
    assert "None" not in caplog.text


def test_album_mode_friendly_qobuz_error_does_not_leak_response_body(caplog):
    # The raw Qobuz response body (JSON / HTML) must not appear in user-facing
    # logs — only the friendly message.
    import types

    from qobuz_librarian.api.auth import QobuzError
    from qobuz_librarian.modes import album as album_mod
    args = types.SimpleNamespace(
        query="anything", dry_run=False, force=False, yes=True, no_import=False,
        verbose=False, consolidate=False, no_upgrade=False, prefer_hires=False,
        no_compress=False, include_singles=False, auto_safe=False, upgrade_walk=False)
    raw = 'HTTP 404 from album/get: {"status":"error","message":"secret"}'
    with patch.object(album_mod, "resolve_album_from_args",
                      side_effect=QobuzError(raw)), \
         patch.object(album_mod, "clear_scan_caches"), \
         patch.object(album_mod, "friendly_qobuz_error",
                      return_value="No album with that id."):
        with caplog.at_level("INFO", logger="qobuz_librarian"):
            with pytest.raises(SystemExit):
                album_mod.run_album_mode(args, "tok", loop=False)
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "No album with that id." in messages
    assert '{"status":"error"' not in messages


def test_cli_drops_to_puid_when_exec_as_root(monkeypatch):
    # `docker exec ... qobuz-librarian` runs as root, bypassing the entrypoint's
    # gosu drop. The CLI re-execs under PUID/PGID to avoid root-owned files;
    # an already-unprivileged run is left alone.
    import qobuz_librarian.cli as cli
    monkeypatch.setenv("PUID", "1000")
    monkeypatch.setenv("PGID", "1000")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/sbin/gosu")
    calls = []
    monkeypatch.setattr("os.execvp", lambda path, argv: calls.append((path, argv)))

    monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
    cli._maybe_drop_privileges()
    assert calls == []

    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    cli._maybe_drop_privileges()
    assert len(calls) == 1
    path, argv = calls[0]
    assert path.endswith("gosu") and argv[1] == "1000:1000"
