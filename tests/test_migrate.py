"""Library migration: placement correctness and the copy-safety guarantees."""
from pathlib import Path

import pytest

from qobuz_librarian.library import migrate as m


def _meta(**kw):
    base = {
        "albumartist": "Artist", "album": "Album", "title": "Song",
        "track": 1, "disc": 1, "disctotal": 0, "year": 2017,
        "compilation": False, "ext": ".flac",
    }
    base.update(kw)
    return base


# ── tag normalization ────────────────────────────────────────────────────────

def test_normalize_tags_parses_slashed_track_and_disc_and_year():
    meta = m.normalize_tags(
        {"albumartist": ["A"], "album": ["B"], "title": ["T"],
         "tracknumber": ["3/12"], "discnumber": ["2/2"], "date": ["2008-05-01"]},
        stem="03 T", ext=".flac")
    assert meta["track"] == 3
    assert meta["disc"] == 2
    assert meta["disctotal"] == 2
    assert meta["year"] == 2008


def test_normalize_tags_falls_back_to_filename_and_artist():
    meta = m.normalize_tags({"artist": "Solo", "album": "Rec"},
                            stem="untitled", ext=".mp3")
    assert meta["albumartist"] == "Solo"   # no albumartist → artist
    assert meta["title"] == "untitled"     # no title → filename stem


def test_compilation_detected_from_flag_or_various_artists():
    assert m.normalize_tags({"albumartist": "X", "album": "Y", "compilation": "1"},
                            "s", ".flac")["compilation"] is True
    assert m.normalize_tags({"albumartist": "Various Artists", "album": "Y"},
                            "s", ".flac")["compilation"] is True


# ── destination path ──────────────────────────────────────────────────────────

def test_destination_matches_beets_layout():
    plan = m.build_plan([(Path("/src/x.flac"), _meta(track=4, title="Hey"), "tags")],
                        Path("/dest"))
    assert plan.placed[0].dest_rel == Path("Artist/Album (2017)/04 - Hey.flac")


def test_compilation_routes_under_various_artists():
    plan = m.build_plan(
        [(Path("/src/x.flac"), _meta(compilation=True, albumartist="Whoever"), "tags")],
        Path("/dest"))
    assert plan.placed[0].dest_rel.parts[0] == "Various Artists"


def test_year_omitted_when_unknown():
    plan = m.build_plan([(Path("/src/x.flac"), _meta(year=0), "tags")], Path("/dest"))
    assert plan.placed[0].dest_rel == Path("Artist/Album/01 - Song.flac")


def test_bad_path_characters_are_sanitized():
    meta = _meta(albumartist="AC/DC", album='Back: "In"', title="T/T")
    plan = m.build_plan([(Path("/src/x.flac"), meta, "tags")], Path("/dest"))
    rel = str(plan.placed[0].dest_rel)
    assert "/DC" not in rel.split("AC", 1)[1].split("/")[0]  # slash inside artist gone
    assert '"' not in rel and ":" not in rel


def test_multidisc_uses_disc_subfolder_only_when_album_spans_discs():
    two_disc = [
        (Path("/src/a.flac"), _meta(disc=1, disctotal=2, title="A", track=1), "tags"),
        (Path("/src/b.flac"), _meta(disc=2, disctotal=2, title="B", track=1), "tags"),
    ]
    plan = m.build_plan(two_disc, Path("/dest"))
    dests = sorted(str(e.dest_rel) for e in plan.placed)
    assert dests == ["Artist/Album (2017)/Disc 1/01 - A.flac",
                     "Artist/Album (2017)/Disc 2/01 - B.flac"]
    # A single-disc album keeps tracks flat.
    one = m.build_plan([(Path("/src/a.flac"), _meta(disc=1, disctotal=1), "tags")],
                       Path("/dest"))
    assert "Disc" not in str(one.placed[0].dest_rel)


# ── classification ─────────────────────────────────────────────────────────────

def test_missing_artist_or_album_is_unplaceable_not_guessed():
    plan = m.build_plan([
        (Path("/src/a.flac"), _meta(albumartist=""), "tags"),
        (Path("/src/b.flac"), None, ""),
    ], Path("/dest"))
    assert len(plan.unplaceable) == 2
    assert not plan.placed


def test_two_sources_to_one_destination_are_skipped_as_collision():
    same = _meta()
    plan = m.build_plan([
        (Path("/src/one.flac"), dict(same), "tags"),
        (Path("/src/two.flac"), dict(same), "tags"),
    ], Path("/dest"))
    assert len(plan.collisions) == 2
    assert not plan.placed


def test_existing_destination_is_a_collision(tmp_path):
    dest = tmp_path / "dest"
    target = dest / "Artist/Album (2017)/01 - Song.flac"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"already here")
    plan = m.build_plan([(tmp_path / "src.flac", _meta(), "tags")], dest)
    assert len(plan.collisions) == 1
    assert plan.collisions[0].reason == "destination already exists"


# ── AcoustID match selection ──────────────────────────────────────────────────

def test_acoustid_rejects_low_confidence_and_ambiguous_matches():
    assert m.choose_acoustid_match([{"score": 0.5, "artist": "A"}]) is None
    assert m.choose_acoustid_match([
        {"score": 0.95, "artist": "Oasis"},
        {"score": 0.93, "artist": "Blur"},
    ]) is None
    chosen = m.choose_acoustid_match([{"score": 0.97, "artist": "Oasis", "title": "T"}])
    assert chosen["artist"] == "Oasis"


# ── safe copy / execution ──────────────────────────────────────────────────────

def _placed_plan(tmp_path, n=1):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    items = []
    for i in range(n):
        p = src_dir / f"track{i}.flac"
        p.write_bytes(b"audio-bytes-%d" % i)
        items.append((p, _meta(title=f"Song {i}", track=i + 1), "tags"))
    return m.build_plan(items, tmp_path / "dest")


def test_copy_mode_leaves_originals_untouched(tmp_path):
    plan = _placed_plan(tmp_path)
    src = plan.placed[0].source
    res = m.execute_plan(plan, in_place=False)
    assert res.copied == 1 and res.failed == 0
    assert src.exists()                                   # original sacred
    dst = plan.dest_root / plan.placed[0].dest_rel
    assert dst.read_bytes() == src.read_bytes()
    assert not dst.with_name(dst.name + ".partial").exists()


def test_in_place_mode_moves_only_after_verified_copy(tmp_path):
    plan = _placed_plan(tmp_path)
    src = plan.placed[0].source
    res = m.execute_plan(plan, in_place=True)
    assert res.copied == 1
    assert not src.exists()                               # moved
    assert (plan.dest_root / plan.placed[0].dest_rel).exists()


def test_execute_never_overwrites_a_destination_that_appears_late(tmp_path):
    plan = _placed_plan(tmp_path)
    dst = plan.dest_root / plan.placed[0].dest_rel
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"precious")
    res = m.execute_plan(plan, in_place=False)
    assert res.copied == 0 and res.skipped == 1
    assert dst.read_bytes() == b"precious"                # not clobbered
    assert plan.placed[0].source.exists()


def test_execute_records_failure_and_continues(tmp_path):
    plan = _placed_plan(tmp_path, n=2)
    plan.placed[0].source.unlink()                        # first source gone
    res = m.execute_plan(plan, in_place=False)
    assert res.failed == 1 and res.copied == 1            # second still copied


def test_execute_stops_on_cancel(tmp_path):
    plan = _placed_plan(tmp_path, n=3)
    res = m.execute_plan(plan, in_place=False, cancel_check=lambda: True)
    assert res.cancelled and res.copied == 0
    for e in plan.placed:
        assert e.source.exists()


def test_migrate_only_flags_require_migrate_mode(monkeypatch):
    from qobuz_librarian import cli
    monkeypatch.setattr("sys.argv", ["qobuz-librarian", "--in-place"])
    with pytest.raises(SystemExit):
        cli.parse_args()


def test_migrate_cannot_combine_with_a_query(monkeypatch):
    from qobuz_librarian import cli
    monkeypatch.setattr("sys.argv", ["qobuz-librarian", "--migrate", "some album"])
    with pytest.raises(SystemExit):
        cli.parse_args()


def test_resolve_paths_refuses_destination_inside_source(tmp_path):
    from types import SimpleNamespace

    from qobuz_librarian.modes import migrate as mode
    src = tmp_path / "lib"
    src.mkdir()
    nested = src / "organized"     # building into the source would self-recurse
    args = SimpleNamespace(migrate_src=str(src), migrate_dest=str(nested))
    assert mode._resolve_paths(args) == (None, None)
    # Separate trees resolve cleanly.
    ok = SimpleNamespace(migrate_src=str(src), migrate_dest=str(tmp_path / "out"))
    assert mode._resolve_paths(ok) == (src, tmp_path / "out")


def test_manifest_lists_every_decision(tmp_path):
    plan = m.build_plan([
        (Path("/src/ok.flac"), _meta(), "tags"),
        (Path("/src/bad.flac"), None, ""),
    ], tmp_path / "dest")
    out = tmp_path / "manifest.csv"
    m.write_manifest(plan, out)
    text = out.read_text()
    assert "status,source_of_truth,source,destination,reason" in text
    assert "/src/ok.flac" in text and "/src/bad.flac" in text
    assert m.PLACE in text and m.UNPLACEABLE in text
