"""Tests for ui_cli/menu.py, ui_cli/prompts.py, and repair_log.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qobuz_fetch.modes.walk import (
    _album_seen_key,
    load_album_walk_seen,
    load_walk_seen,
    record_album_walk_seen,
    record_walk_seen,
)
from qobuz_fetch.repair_log import append_repair_log, scan_dir_for_isrc_repairs
from qobuz_fetch.ui_cli.menu import interactive_session_mode
from qobuz_fetch.ui_cli.prompts import (
    _read_fetch_log,
    confirm,
    interactive_query,
    log_fetch,
    parse_number_list,
    print_album_summary,
    prompt_album_selection,
)


class TestInteractiveSessionMode:
    def _run(self, inputs):
        with patch("builtins.input", side_effect=inputs):
            return interactive_session_mode()

    def test_blank_returns_album(self):
        assert self._run([""]) == "album"

    def test_2_returns_artist(self):
        assert self._run(["2"]) == "artist"

    def test_q_returns_quit(self):
        assert self._run(["q"]) == "quit"

    def test_garbage_reprompts_then_valid(self):
        assert self._run(["xyz", "2"]) == "artist"

    def test_7_returns_upgrade(self):
        assert self._run(["7"]) == "upgrade"

    def test_3_returns_walk(self):
        assert self._run(["3"]) == "walk"

    def test_4_returns_walk_queue(self):
        assert self._run(["4"]) == "walk_queue"

    def test_5_returns_album_walk(self):
        assert self._run(["5"]) == "album_walk"

    def test_6_returns_album_repair(self):
        assert self._run(["6"]) == "album_repair"


class TestInteractiveQueryAdvertisesHelp:
    def test_prompt_advertises_question_mark(self):
        fake = MagicMock(return_value="")
        with patch("builtins.input", fake):
            assert interactive_query() is None
        assert any("?=recent" in str(c.args) for c in fake.call_args_list)

    def test_question_mark_shows_recent_then_reprompts(self):
        from qobuz_fetch.ui_cli import prompts as p
        with patch.object(p, "show_recent_fetches") as fake_show:
            with patch("builtins.input", side_effect=["?", ""]):
                assert p.interactive_query() is None
        assert fake_show.call_count == 1


class TestConfirm:
    def test_auto_yes_returns_true(self):
        assert confirm("Do it?", auto_yes=True) is True

    def test_y_returns_true(self):
        with patch("builtins.input", return_value="y"):
            assert confirm("Do it?") is True

    def test_eof_returns_false(self):
        with patch("builtins.input", side_effect=EOFError):
            assert confirm("Do it?") is False


class TestParseNumberList:
    def test_single_number(self):
        assert parse_number_list("3", 5) == [3]

    def test_range(self):
        assert parse_number_list("2-4", 5) == [2, 3, 4]

    def test_all_returns_all(self):
        assert parse_number_list("all", 3) == [1, 2, 3]

    def test_out_of_range_clamped(self):
        assert max(parse_number_list("3-10", 5)) <= 5


class TestFetchLog:
    def test_round_trip_single_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr("qobuz_fetch.config.FETCH_LOG_FILE", tmp_path / "log.json")
        log_fetch({"ts": "2026-01-01", "artist": "Artist", "title": "Album"})
        entries = _read_fetch_log()
        assert len(entries) == 1 and entries[0]["artist"] == "Artist"

    def test_returns_empty_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("qobuz_fetch.config.FETCH_LOG_FILE", tmp_path / "absent.json")
        assert _read_fetch_log() == []

    def test_skips_malformed_jsonl_line(self, tmp_path, monkeypatch):
        f = tmp_path / "log.json"
        f.write_text('{"artist":"A"}\nNOT JSON\n{"artist":"B"}\n')
        monkeypatch.setattr("qobuz_fetch.config.FETCH_LOG_FILE", f)
        assert len(_read_fetch_log()) == 2


class TestAppendRepairLog:
    def test_returns_true_on_success(self, tmp_path, monkeypatch):
        f = tmp_path / "repair.log"
        monkeypatch.setattr("qobuz_fetch.config.REPAIR_LOG_PATH", f)
        assert append_repair_log([{"artist": "A", "album": "B", "title": "T"}]) is True
        assert f.exists()

    def test_no_header_on_second_write(self, tmp_path, monkeypatch):
        f = tmp_path / "repair.log"
        monkeypatch.setattr("qobuz_fetch.config.REPAIR_LOG_PATH", f)
        append_repair_log([{"artist": "A", "album": "B", "title": "T1"}])
        append_repair_log([{"artist": "A", "album": "B", "title": "T2"}])
        assert f.read_text().count("# Replaced-tracks log") == 1

    def test_pipe_char_escaped_in_artist(self, tmp_path, monkeypatch):
        f = tmp_path / "repair.log"
        monkeypatch.setattr("qobuz_fetch.config.REPAIR_LOG_PATH", f)
        append_repair_log([{"artist": "AC|DC", "album": "Back in Black", "title": "Hells Bells"}])
        assert "AC|DC" not in f.read_text().split("\n")[-2]

    def test_concurrent_appends_produce_parseable_output(self, tmp_path, monkeypatch):
        """Run-lock currently serializes appenders, but the log itself must
        stay parseable if a future code path ever writes outside that scope.
        Spawn many threads and assert: exactly one header, every data line
        present, no interleaving."""
        from concurrent.futures import ThreadPoolExecutor

        f = tmp_path / "repair.log"
        monkeypatch.setattr("qobuz_fetch.config.REPAIR_LOG_PATH", f)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(
                append_repair_log,
                [{"artist": f"Artist{i}", "album": "B", "title": f"Title{i}"}],
            ) for i in range(40)]
            assert all(fut.result(timeout=5) for fut in futures)
        text = f.read_text()
        assert text.count("# Replaced-tracks log") == 1
        data_lines = [ln for ln in text.splitlines()
                      if ln and not ln.startswith("#")]
        assert len(data_lines) == 40
        titles_seen = {ln.rsplit("|", 1)[-1].strip() for ln in data_lines}
        assert titles_seen == {f"Title{i}" for i in range(40)}


class TestScanDirForIsrcRepairs:
    def _make_track(self, isrc="GB1234567890", length=240.0):
        return {"isrc": isrc, "length": length, "title": "Track", "path": "/music/track.flac"}

    def test_empty_dir_returns_empty(self, tmp_path):
        with patch("qobuz_fetch.repair_log.read_album_dir", return_value=[]):
            result = scan_dir_for_isrc_repairs(tmp_path, "token")
        assert result["verified_truncated"] == [] and result["verified_ok"] == 0

    def test_both_gates_must_fire_for_truncation(self, tmp_path):
        track = self._make_track(length=169.0)
        qt = {"duration": 200.0, "title": "T", "track_number": 1}
        with patch("qobuz_fetch.repair_log.read_album_dir", return_value=[track]):
            with patch("qobuz_fetch.repair_log.find_qobuz_track_by_isrc", return_value=qt):
                result = scan_dir_for_isrc_repairs(tmp_path, "token")
        assert len(result["verified_truncated"]) == 1

    def test_normal_duration_goes_to_verified_ok(self, tmp_path):
        track = self._make_track(length=230.0)
        qt = {"duration": 240.0, "title": "T", "track_number": 1}
        with patch("qobuz_fetch.repair_log.read_album_dir", return_value=[track]):
            with patch("qobuz_fetch.repair_log.find_qobuz_track_by_isrc", return_value=qt):
                result = scan_dir_for_isrc_repairs(tmp_path, "token")
        assert result["verified_ok"] == 1 and result["verified_truncated"] == []

    def test_zero_qobuz_duration_treated_as_ok(self, tmp_path):
        track = self._make_track(length=10.0)
        qt = {"duration": 0, "title": "T", "track_number": 1}
        with patch("qobuz_fetch.repair_log.read_album_dir", return_value=[track]):
            with patch("qobuz_fetch.repair_log.find_qobuz_track_by_isrc", return_value=qt):
                result = scan_dir_for_isrc_repairs(tmp_path, "token")
        assert result["verified_ok"] == 1


class TestScanDirRealFlacRoundTrip:

    def _make_flac(self, path, seconds, isrc):
        import subprocess
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", str(seconds), "-c:a", "flac", str(path)],
            check=True,
        )
        from mutagen.flac import FLAC
        f = FLAC(str(path))
        f["isrc"] = [isrc]
        f["title"] = ["Some Track"]
        f["tracknumber"] = ["1"]
        f.save()

    @pytest.fixture(autouse=True)
    def _need_ffmpeg(self):
        import shutil
        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg not available")

    def test_real_short_flac_flagged_truncated(self, tmp_path):
        album = tmp_path / "Artist" / "Album"
        album.mkdir(parents=True)
        self._make_flac(album / "01.flac", 4, "US1234500001")
        qt = {"duration": 200.0, "title": "Some Track", "track_number": 1}
        with patch("qobuz_fetch.repair_log.find_qobuz_track_by_isrc", return_value=qt):
            result = scan_dir_for_isrc_repairs(album, "token")
        assert len(result["verified_truncated"]) == 1


class TestParseArgsGuards:
    def _parse(self, argv):
        import sys

        from qobuz_fetch.cli import parse_args
        with patch.object(sys, "argv", ["qobuz-librarian", *argv]):
            return parse_args()

    def test_auto_safe_without_upgrade_walk_accepted(self):
        args = self._parse(["--auto-safe"])
        assert args.auto_safe is True
        assert args.upgrade_walk is False

    def test_force_with_artist_rejected(self):
        with pytest.raises(SystemExit):
            self._parse(["--force", "--artist", "Radiohead"])

    def test_force_in_album_mode_allowed(self):
        args = self._parse(["--force", "Some Artist - Album"])
        assert args.force is True

    def test_include_singles_with_artist_allowed(self):
        args = self._parse(["--include-singles", "--artist", "Radiohead"])
        assert args.include_singles is True

    def test_no_catalog_in_album_mode_rejected(self):
        with pytest.raises(SystemExit):
            self._parse(["--no-catalog", "Some Artist - Album"])

    def test_include_comps_without_artist_rejected(self):
        with pytest.raises(SystemExit):
            self._parse(["--include-comps", "--upgrade-walk"])

    def test_no_upgrade_with_upgrade_walk_rejected(self):
        with pytest.raises(SystemExit):
            self._parse(["--no-upgrade", "--upgrade-walk"])

    def test_include_singles_with_upgrade_walk_rejected(self):
        with pytest.raises(SystemExit):
            self._parse(["--include-singles", "--upgrade-walk"])

    def test_dry_run_yes_no_import_no_color_accepted(self):
        args = self._parse(["--dry-run", "--yes", "--no-import", "--no-color"])
        assert args.dry_run is True
        assert args.yes is True
        assert args.no_import is True
        assert args.no_color is True

    def test_no_prefer_hires_overrides_default(self):
        args = self._parse(["--no-prefer-hires", "Some Artist - Album"])
        assert args.prefer_hires is False

    def test_no_compress_no_consolidate_no_migrate(self):
        args = self._parse(["--no-compress", "--no-consolidate",
                            "--no-migrate-multi-artist", "Q"])
        assert args.no_compress is True
        assert args.consolidate is False
        assert args.migrate_multi_artist is False

    def test_upgrade_walk_alone_accepted(self):
        args = self._parse(["--upgrade-walk"])
        assert args.upgrade_walk is True


class TestScanETA:
    def test_eta_empty_before_first_item(self):
        from qobuz_fetch.web.flows import _fmt_eta
        assert _fmt_eta(0.0, 0, 10) == ""

    def test_eta_seconds_format(self, monkeypatch):
        import time as _t

        from qobuz_fetch.web import flows
        # 1 done at t=2s, 9 to go → ETA 18s
        monkeypatch.setattr(_t, "monotonic", lambda: 2.0)
        eta = flows._fmt_eta(0.0, 1, 10)
        assert eta == " (eta: 18s)"

    def test_eta_minutes_format(self, monkeypatch):
        import time as _t

        from qobuz_fetch.web import flows
        # 5 done at t=60s, 95 to go → ETA 1140s = 19m 0s
        monkeypatch.setattr(_t, "monotonic", lambda: 60.0)
        eta = flows._fmt_eta(0.0, 5, 100)
        assert eta == " (eta: 19m 0s)"


class TestFileLogging:
    def test_attach_file_handler_writes_to_file(self, tmp_path):
        from qobuz_fetch.ui_cli import logging as qlog
        log_path = tmp_path / "qobuz-librarian.log"
        # Reset _file_handler so the call actually runs.
        if qlog._file_handler is not None:
            qlog.log.removeHandler(qlog._file_handler)
            qlog._file_handler = None
        qlog.attach_file_handler(log_path, "INFO")
        qlog.log.info("\x1b[31mred message\x1b[0m")
        qlog._file_handler.flush()
        contents = log_path.read_text()
        # ANSI stripped, message persisted.
        assert "red message" in contents
        assert "\x1b" not in contents
        # Clean up the handler so we don't affect other tests.
        qlog.log.removeHandler(qlog._file_handler)
        qlog._file_handler = None


class TestQuietFlag:
    def test_quiet_raises_logger_threshold_above_info(self):
        import logging

        from qobuz_fetch.ui_cli.logging import log, set_quiet
        set_quiet(True)
        try:
            assert log.level == logging.WARNING
        finally:
            set_quiet(False)
            assert log.level == logging.INFO


class TestExitCodes:
    def test_die_uses_provided_code(self, capsys):
        from qobuz_fetch.ui_cli.errors import EXIT_AUTH, die
        with pytest.raises(SystemExit) as ei:
            die("auth failure msg", EXIT_AUTH)
        assert ei.value.code == EXIT_AUTH
        assert "auth failure msg" in capsys.readouterr().err

    def test_die_default_code_is_general(self):
        from qobuz_fetch.ui_cli.errors import EXIT_GENERAL, die
        with pytest.raises(SystemExit) as ei:
            die("x")
        assert ei.value.code == EXIT_GENERAL


class TestParseQobuzURL:
    def _p(self, url):
        from qobuz_fetch.cli import parse_qobuz_url
        return parse_qobuz_url(url)

    def test_play_url(self):
        assert self._p("https://play.qobuz.com/album/abc12345") == ("album", "abc12345")

    def test_play_track(self):
        assert self._p("https://play.qobuz.com/track/9876543") == ("track", "9876543")

    def test_store_album(self):
        assert self._p("https://www.qobuz.com/us-en/album/some-title/abc123") == ("album", "abc123")

    def test_store_track(self):
        assert self._p("https://www.qobuz.com/us-en/track/some-title/xyz789") == ("track", "xyz789")

    def test_non_qobuz_returns_none(self):
        assert self._p("https://example.com/something") is None

    def test_garbage_returns_none(self):
        assert self._p("not a url") is None


def test_interactive_query_warns_on_non_qobuz_url(caplog):
    """A pasted non-Qobuz http URL must warn and re-prompt, not text-search it."""
    import logging

    from qobuz_fetch.ui_cli.prompts import interactive_query

    with patch("builtins.input",
               side_effect=["https://example.com/album/123", "q"]):
        with caplog.at_level(logging.INFO, logger="qobuz_librarian"):
            result = interactive_query()
    assert result is None  # user cancelled at second prompt
    assert any("Only Qobuz URLs" in r.message for r in caplog.records)


def test_album_mode_track_url_at_interactive_prompt_explains_clearly(capsys):
    """A track URL at the interactive prompt must say so, not "Bad URL"."""
    import types

    from qobuz_fetch.modes.album import run_album_mode

    args = types.SimpleNamespace(
        query=[], dry_run=False, force=False, yes=False,
        no_import=False, verbose=False, consolidate=False,
        no_upgrade=False, prefer_hires=False, no_compress=False,
        include_singles=False, auto_safe=False, upgrade_walk=False,
    )

    with patch("qobuz_fetch.modes.album.interactive_query",
               return_value=("__url__", "https://play.qobuz.com/track/12345")), \
         patch("qobuz_fetch.modes.album.clear_scan_caches"):
        with pytest.raises(SystemExit):
            run_album_mode(args, "tok", loop=False)
    assert "track URL" in capsys.readouterr().err


def test_album_mode_aborted_at_query_prompt_breaks_loop():
    """When the top-level query prompt raises Aborted, the loop exits
    without ever reaching the download path."""
    import types

    from qobuz_fetch.api.auth import Aborted
    from qobuz_fetch.modes.album import run_album_mode

    album = {"id": "A1", "title": "Album", "artist": {"name": "Artist"}}
    args = types.SimpleNamespace(
        query=["abbey", "road"], dry_run=False, force=False, yes=False,
        no_import=False, verbose=False, consolidate=False,
        no_upgrade=False, prefer_hires=False, no_compress=False,
        include_singles=False, auto_safe=False, upgrade_walk=False,
    )

    resolve_calls = []
    def fake_resolve(a, tok):
        resolve_calls.append(1)
        if len(resolve_calls) == 1:
            return album
        raise Aborted("loop exit")

    with patch("qobuz_fetch.modes.album.resolve_album_from_args",
               side_effect=fake_resolve), \
         patch("qobuz_fetch.modes.album._interactive_album_action"), \
         patch("qobuz_fetch.modes.album.process_album") as mock_process:
        run_album_mode(args, "tok", loop=True)

    assert mock_process.called is False


class TestRepairBackupResolution:
    """repair_album_dir's backup-resolution branches: full success drops the
    backup, download failure preserves it, silent beets failure auto-restores
    the truncated originals so the library returns to its pre-repair state."""

    def _call_repair(self, tmp_path, monkeypatch, *, n_ok, n_fail, imported):
        from argparse import Namespace

        import qobuz_fetch.modes.repair as repair_mod

        album_dir = tmp_path / "Artist" / "Album (2020)"
        album_dir.mkdir(parents=True)
        track = album_dir / "01 - Track.flac"
        track.write_bytes(b"\x00" * 200)

        monkeypatch.setattr("qobuz_fetch.config.UPGRADE_BACKUP_DIR", tmp_path / "backups")
        monkeypatch.setattr("qobuz_fetch.config.REPAIR_LOG_PATH", tmp_path / "repair.log")
        monkeypatch.setattr(repair_mod, "get_album",
                            lambda aid, tok: {"id": aid, "title": "Album",
                                             "tracks": {"items": []}})

        def fake_execute_queue(queue, args, token):
            for qi in queue:
                qi["n_ok"] = n_ok
                qi["n_fail"] = n_fail
                qi["imported"] = imported

        monkeypatch.setattr(repair_mod, "_execute_download_queue", fake_execute_queue)
        monkeypatch.setattr(repair_mod, "append_repair_log", lambda e: True)

        vt = [{"path": str(track), "title": "Track 01",
               "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
               "file_length": 5.0}]
        args = Namespace(force=False, yes=True, prefer_hires=False,
                         consolidate=False, no_upgrade=False)
        from qobuz_fetch.modes.repair import repair_album_dir
        return repair_album_dir(album_dir, vt, "Artist", args, "tok")

    def _backup_files(self, tmp_path):
        backup_root = tmp_path / "backups"
        if not backup_root.exists():
            return []
        return [p for p in backup_root.rglob("*") if p.is_file()]

    def test_backup_dropped_on_full_success(self, tmp_path, monkeypatch):
        result = self._call_repair(tmp_path, monkeypatch, n_ok=1, n_fail=0, imported=True)
        assert self._backup_files(tmp_path) == []
        assert result["n_ok"] == 1

    def test_backup_restored_on_silent_beets_failure(self, tmp_path, monkeypatch):
        """When beets silently fails (n_fail==0, imported==False), the
        original tracks are restored to album_dir from the upgrade backup."""
        result = self._call_repair(tmp_path, monkeypatch, n_ok=1, n_fail=0, imported=False)
        assert self._backup_files(tmp_path) == []
        assert (tmp_path / "Artist" / "Album (2020)" / "01 - Track.flac").exists()
        assert result["imported"] is False

    def test_backup_kept_when_downloads_fail(self, tmp_path, monkeypatch):
        self._call_repair(tmp_path, monkeypatch, n_ok=0, n_fail=1, imported=False)
        assert self._backup_files(tmp_path)

    def test_backup_failure_leaves_original_intact(self, tmp_path, monkeypatch):
        """When backup_gap_fill_files returns None, repair aborts without
        unlinking the original files or running the download queue."""
        from argparse import Namespace

        import qobuz_fetch.modes.repair as repair_mod

        album_dir = tmp_path / "Artist" / "Album (2020)"
        album_dir.mkdir(parents=True)
        track = album_dir / "01 - Track.flac"
        track.write_bytes(b"\x00" * 200)

        monkeypatch.setattr(repair_mod, "backup_gap_fill_files", lambda paths, d: None)

        def _should_not_run(queue, args, token):
            raise AssertionError("download queue must not run when backup fails")

        monkeypatch.setattr(repair_mod, "_execute_download_queue", _should_not_run)

        vt = [{"path": str(track), "title": "Track 01",
               "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "ALB1"}},
               "file_length": 5.0}]
        args = Namespace(force=False, yes=True, prefer_hires=False,
                         consolidate=False, no_upgrade=False)
        result = repair_mod.repair_album_dir(album_dir, vt, "Artist", args, "tok")

        assert track.exists()
        assert result["n_fail"] == len(vt)


class TestWalkSeenFile:
    def test_parses_written_artist(self, tmp_path, monkeypatch):
        f = tmp_path / "walk_seen.txt"
        monkeypatch.setattr("qobuz_fetch.config.WALK_SEEN_FILE", f)
        record_walk_seen("Radiohead")
        assert "radiohead" in load_walk_seen()

    def test_record_is_idempotent(self, tmp_path, monkeypatch):
        f = tmp_path / "walk_seen.txt"
        monkeypatch.setattr("qobuz_fetch.config.WALK_SEEN_FILE", f)
        record_walk_seen("Beatles")
        record_walk_seen("Beatles")
        lines = [l for l in f.read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
        assert lines.count("Beatles") == 1

    def test_second_artist_appended_not_overwritten(self, tmp_path, monkeypatch):
        f = tmp_path / "walk_seen.txt"
        monkeypatch.setattr("qobuz_fetch.config.WALK_SEEN_FILE", f)
        record_walk_seen("Radiohead")
        record_walk_seen("Portishead")
        seen = load_walk_seen()
        assert "radiohead" in seen and "portishead" in seen

    def test_interrupted_write_preserves_prior_entries(self, tmp_path, monkeypatch):
        """A crash during the rename step must leave the file unchanged,
        not half-written. The previous append-mode write could leave a
        truncated line that the loader silently dropped."""
        f = tmp_path / "walk_seen.txt"
        monkeypatch.setattr("qobuz_fetch.config.WALK_SEEN_FILE", f)
        record_walk_seen("Radiohead")
        prior_bytes = f.read_bytes()

        import qobuz_fetch.modes.walk as walk_mod
        monkeypatch.setattr(walk_mod.os, "replace",
                            lambda *_a: (_ for _ in ()).throw(OSError("crashed")))
        record_walk_seen("Portishead")

        assert f.read_bytes() == prior_bytes
        assert load_walk_seen() == {"radiohead"}


class TestAlbumWalkSeenFile:
    def test_album_seen_key_normalizes(self):
        assert _album_seen_key("AC/DC", "Highway to Hell") == "acdc::highwaytohell"

    def test_parses_written_entry(self, tmp_path, monkeypatch):
        f = tmp_path / "album_walk_seen.txt"
        monkeypatch.setattr("qobuz_fetch.config.ALBUM_WALK_SEEN_FILE", f)
        record_album_walk_seen("Radiohead", "OK Computer")
        assert _album_seen_key("Radiohead", "OK Computer") in load_album_walk_seen()

    def test_record_is_idempotent(self, tmp_path, monkeypatch):
        f = tmp_path / "album_walk_seen.txt"
        monkeypatch.setattr("qobuz_fetch.config.ALBUM_WALK_SEEN_FILE", f)
        record_album_walk_seen("Beatles", "Abbey Road")
        record_album_walk_seen("Beatles", "Abbey Road")
        lines = [l for l in f.read_text().splitlines()
                 if " | " in l and not l.startswith("#")]
        assert len(lines) == 1


class TestScanReportRepair:
    def _call(self, tmp_path, monkeypatch, *, repair_result=None,
              verified_truncated=None, yes=True, input_return="y"):
        from argparse import Namespace

        import qobuz_fetch.modes.repair as repair_mod
        from qobuz_fetch.modes.repair import _scan_report_repair

        album_dir = tmp_path / "Artist" / "Album (2022)"
        album_dir.mkdir(parents=True)
        (album_dir / "01 Track.flac").write_bytes(b"\x00" * 200)

        if verified_truncated is None:
            verified_truncated = [{
                "path": str(album_dir / "01 Track.flac"),
                "title": "Track 01", "isrc": "USRC12345678",
                "track_number": 1, "file_length": 5.0, "qobuz_duration": 180.0,
                "qobuz_track": {"id": 1, "title": "Track 01", "album": {"id": "A1"}},
            }]

        monkeypatch.setattr(repair_mod, "scan_dir_for_isrc_repairs",
                            lambda *a, **k: {"verified_truncated": verified_truncated,
                                             "verified_ok": 0, "isrc_no_match": [],
                                             "no_isrc_tag": []})
        if repair_result is not None:
            monkeypatch.setattr(repair_mod, "repair_album_dir",
                                lambda *a, **k: repair_result)
        monkeypatch.setattr(repair_mod, "section", lambda *a: None)

        args = Namespace(force=False, yes=yes, prefer_hires=False,
                         consolidate=False, no_upgrade=False)
        with patch("builtins.input", return_value=input_return):
            return _scan_report_repair(album_dir, "Artist", args, "tok")

    def test_returns_repaired_on_success(self, tmp_path, monkeypatch):
        assert self._call(tmp_path, monkeypatch,
                          repair_result={"n_ok": 1, "n_fail": 0,
                                         "imported": True, "backup": None}) == "repaired"

    def test_returns_failed_when_import_fails(self, tmp_path, monkeypatch):
        assert self._call(tmp_path, monkeypatch,
                          repair_result={"n_ok": 1, "n_fail": 0,
                                         "imported": False, "backup": None}) == "failed"

    def test_returns_failed_when_no_tracks_downloaded(self, tmp_path, monkeypatch):
        assert self._call(tmp_path, monkeypatch,
                          repair_result={"n_ok": 0, "n_fail": 1,
                                         "imported": False, "backup": None}) == "failed"

    def test_returns_clean_when_no_truncated_files(self, tmp_path, monkeypatch):
        assert self._call(tmp_path, monkeypatch, verified_truncated=[]) == "clean"

    def test_returns_skipped_when_user_declines(self, tmp_path, monkeypatch):
        assert self._call(tmp_path, monkeypatch, yes=False, input_return="n") == "skipped"


class TestModeEntryPoints:
    def test_run_album_mode_catalog_miss_returns_cleanly(self):
        """CatalogMiss in non-loop mode causes run_album_mode to return."""
        import types

        from qobuz_fetch.api.auth import CatalogMiss
        from qobuz_fetch.modes.album import run_album_mode

        args = types.SimpleNamespace(
            query="test query", dry_run=False, force=False, yes=True,
            no_import=False, verbose=False, consolidate=False,
            no_upgrade=False, prefer_hires=False, no_compress=False,
            include_singles=False, auto_safe=False, upgrade_walk=False,
        )
        with patch("qobuz_fetch.modes.album.resolve_album_from_args",
                   side_effect=CatalogMiss("not found")), \
             patch("qobuz_fetch.modes.album.clear_scan_caches"):
            result = run_album_mode(args, "tok", loop=False)
        assert result is None

    def test_run_album_repair_mode_cancel_returns_cleanly(self):
        """User cancelling the picker in non-loop mode causes clean return."""
        import types

        from qobuz_fetch.modes.repair import run_album_repair_mode

        args = types.SimpleNamespace(
            query=None, dry_run=False, force=False, yes=False,
            no_import=False, verbose=False, consolidate=False,
            no_upgrade=False, prefer_hires=False, no_compress=False,
        )
        with patch("qobuz_fetch.modes.repair._prompt_library_album_for_repair",
                   return_value=(None, None)), \
             patch("qobuz_fetch.modes.repair.clear_scan_caches"):
            result = run_album_repair_mode(args, "tok", loop=False)
        assert result is None

    def test_run_upgrade_walk_mode_empty_library_returns_cleanly(self):
        """Empty library causes upgrade walk to return without prompting."""
        import types

        from qobuz_fetch.modes.upgrade import run_upgrade_walk_mode

        args = types.SimpleNamespace(
            dry_run=False, yes=False, auto_safe=False, force=False,
            consolidate=False, no_import=False, verbose=False,
            no_compress=False, prefer_hires=False,
        )
        with patch("qobuz_fetch.modes.upgrade.list_library_artists", return_value=[]), \
             patch("qobuz_fetch.modes.upgrade.clear_scan_caches"):
            result = run_upgrade_walk_mode(args, "tok")
        assert result is None


class TestAlbumModeEntry:
    def test_calls_process_album_on_success(self):
        import types

        from qobuz_fetch.modes.album import run_album_mode

        album = {"id": "A1", "title": "Abbey Road",
                 "artist": {"name": "The Beatles"}}
        args = types.SimpleNamespace(
            query="abbey road", dry_run=False, force=False, yes=True,
            no_import=False, verbose=False, consolidate=False,
            no_upgrade=False, prefer_hires=False, no_compress=False,
            include_singles=False, auto_safe=False, upgrade_walk=False,
        )
        with patch("qobuz_fetch.modes.album.resolve_album_from_args",
                   return_value=album) as mock_resolve, \
             patch("qobuz_fetch.modes.album.process_album") as mock_process, \
             patch("qobuz_fetch.modes.album.clear_scan_caches"):
            run_album_mode(args, "tok", loop=False)

        mock_resolve.assert_called_once()
        mock_process.assert_called_once()
        assert mock_process.call_args[0][0] is album

    def test_qobuz_error_exits_nonzero(self):
        import types

        from qobuz_fetch.api.auth import QobuzError
        from qobuz_fetch.modes.album import run_album_mode

        args = types.SimpleNamespace(
            query="anything", dry_run=False, force=False, yes=True,
            no_import=False, verbose=False, consolidate=False,
            no_upgrade=False, prefer_hires=False, no_compress=False,
            include_singles=False, auto_safe=False, upgrade_walk=False,
        )
        with patch("qobuz_fetch.modes.album.resolve_album_from_args",
                   side_effect=QobuzError("503")), \
             patch("qobuz_fetch.modes.album.clear_scan_caches"):
            with pytest.raises(SystemExit) as exc:
                run_album_mode(args, "tok", loop=False)
        assert exc.value.code == 1



class TestNullTitleDisplay:

    NULL_ALBUM = {
        "id": "1", "title": None, "artist": None,
        "tracks_count": 10, "maximum_bit_depth": 16,
        "maximum_sampling_rate": 44.1, "released_at": 0,
    }

    def test_print_album_summary_null_title_shows_placeholder(self, caplog):
        import logging
        with caplog.at_level(logging.INFO):
            print_album_summary(
                self.NULL_ALBUM,
                missing=[],
                present=[{"id": "t1"}],
                album_dir=None,
                force=False,
            )
        combined = caplog.text
        assert "None" not in combined
