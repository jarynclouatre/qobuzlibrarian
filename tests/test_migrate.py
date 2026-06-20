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


def test_validate_paths_rejects_unwritable_destination(tmp_path, monkeypatch):
    # The destination is written to; a read-only dest must fail the preflight,
    # not partway through the migration with a raw PermissionError.
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    assert m.validate_paths(src, dest) is None          # writable dest is fine
    real_access = m.os.access
    monkeypatch.setattr(m.os, "access",
                        lambda p, mode: False
                        if (str(p) == str(dest) and mode == m.os.W_OK)
                        else real_access(p, mode))
    reason = m.validate_paths(src, dest)
    assert reason and "writable" in reason.lower()


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


def test_overlong_tag_components_are_truncated_not_fatal():
    # A ~300-char tag-derived component would exceed the 255-byte NAME_MAX and
    # make Path.exists() raise ENAMETOOLONG, aborting the whole plan. Each
    # component is capped to 255 bytes (extension preserved) so the plan builds.
    long_album = "Z" * 300
    long_title = "T" * 300
    meta = _meta(albumartist="Artist", album=long_album, title=long_title, track=5)
    plan = m.build_plan([(Path("/src/x.flac"), meta, "tags")], Path("/dest"))
    rel = plan.placed[0].dest_rel
    for part in rel.parts:
        assert len(part.encode("utf-8")) <= 255
    assert rel.name.endswith(".flac")           # extension survived truncation
    # A multi-byte tag truncates on a char boundary (no UnicodeDecodeError).
    mb = _meta(albumartist="A", album="名" * 200, title="t", track=1)
    p2 = m.build_plan([(Path("/src/y.flac"), mb, "tags")], Path("/dest"))
    assert all(len(part.encode("utf-8")) <= 255 for part in p2.placed[0].dest_rel.parts)


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


def test_companion_files_are_carried_alongside_audio(tmp_path):
    plan = _placed_plan(tmp_path)
    src_dir = plan.placed[0].source.parent
    (src_dir / "cover.jpg").write_bytes(b"JPEGDATA")
    (src_dir / "album.log").write_bytes(b"rip log")
    (src_dir / "notes.txt").write_bytes(b"not a companion")   # excluded ext
    res = m.execute_plan(plan, in_place=False)
    assert res.copied == 1
    assert res.companions == 2                                # cover.jpg + album.log
    dst_dir = (plan.dest_root / plan.placed[0].dest_rel).parent
    assert (dst_dir / "cover.jpg").read_bytes() == b"JPEGDATA"
    assert (dst_dir / "album.log").exists()
    assert not (dst_dir / "notes.txt").exists()               # .txt isn't carried
    assert (src_dir / "cover.jpg").exists()                   # copy: source untouched


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


def test_results_manifest_records_copied_and_failed(tmp_path):
    plan = _placed_plan(tmp_path, n=2)
    plan.placed[0].source.unlink()                  # one source vanishes
    res = m.execute_plan(plan, in_place=False)
    out = tmp_path / "results.csv"
    m.write_results_manifest(res, out)
    text = out.read_text()
    assert "status,source,destination,reason" in text
    assert m.COPIED in text and m.FAILED in text


def test_space_estimate_counts_copy_bytes_but_not_same_fs_moves(tmp_path):
    plan = _placed_plan(tmp_path, n=2)
    total = sum(e.source.stat().st_size for e in plan.placed)
    need, free = m.space_estimate(plan, in_place=False)
    assert need == total                            # a copy writes every file
    assert free is not None and free > 0
    # An in-place move within one filesystem is a rename — no bytes written.
    assert m.space_estimate(plan, in_place=True)[0] == 0


def test_prune_empty_dirs_clears_husk_but_keeps_root_and_nonempty(tmp_path):
    root = tmp_path / "src"
    (root / "EmptyArtist" / "EmptyAlbum").mkdir(parents=True)   # nested empties
    keep = root / "HasArt"
    keep.mkdir()
    (keep / "cover.jpg").write_bytes(b"art")
    removed = m.prune_empty_dirs(root)
    assert removed == 2                             # album + its now-empty artist
    assert not (root / "EmptyArtist").exists()
    assert (keep / "cover.jpg").exists()            # a folder with a file stays
    assert root.exists()                            # the root is never removed


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


def test_whole_run_modes_are_mutually_exclusive(monkeypatch):
    from qobuz_librarian import cli
    for combo in (["--migrate", "--downsample-walk"],
                  ["--reset-walk-seen", "--lyrics-walk"]):
        monkeypatch.setattr("sys.argv", ["qobuz-librarian", *combo])
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


# ── web flow (scan → review candidates → execute copy) ─────────────────────────

def test_scan_migration_groups_albums_into_candidates(tmp_path, monkeypatch):
    from qobuz_librarian.library import migrate as eng
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    items = [
        (Path("/s/a.flac"), _meta(albumartist="Bonobo", album="Black Sands",
                                  title="Kong", track=1), "tags"),
        (Path("/s/b.flac"), _meta(albumartist="Bonobo", album="Black Sands",
                                  title="Kiara", track=2), "tags"),
        (Path("/s/c.flac"), _meta(albumartist="Beatles", album="Revolver",
                                  title="Taxman", track=1), "tags"),
        (Path("/s/d.flac"), None, ""),     # tag-less → unplaceable
    ]
    monkeypatch.setattr(eng, "collect_items", lambda *a, **k: items)
    job = jm.Job(title="mig")
    flows.scan_migration(job, str(tmp_path / "src"), str(tmp_path / "dest"),
                         use_acoustid=False)
    artists = sorted(c["artist"] for c in job.candidates)
    assert artists == ["Beatles", "Bonobo"]        # one candidate per album
    bonobo = next(c for c in job.candidates if c["artist"] == "Bonobo")
    assert "2 track" in bonobo["detail"]
    assert "1 couldn't be identified" in job.summary
    assert (tmp_path / "dest" / "migration-manifest.csv").exists()


def test_execute_migration_copies_selected_and_keeps_originals(tmp_path):
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    src = tmp_path / "src"
    src.mkdir()
    f1, f2 = src / "x.flac", src / "y.flac"
    f1.write_bytes(b"one")
    f2.write_bytes(b"two")
    dest = tmp_path / "dest"
    chosen = [{"payload": {"entries": [
        (str(f1), "Artist/Album (2017)/01 - A.flac"),
        (str(f2), "Artist/Album (2017)/02 - B.flac"),
    ]}}]
    job = jm.Job(title="mig")
    flows.execute_migration(job, chosen, str(dest), in_place=False)
    assert (dest / "Artist/Album (2017)/01 - A.flac").read_bytes() == b"one"
    assert (dest / "Artist/Album (2017)/02 - B.flac").read_bytes() == b"two"
    assert f1.exists() and f2.exists()             # copy mode: originals intact
    assert "2 files copied" in job.summary
    assert (dest / "migration-results.csv").exists()


def test_resume_migration_passes_the_persisted_src(monkeypatch):
    # A restart resumes from execute_args; src must survive so an in-place move
    # still prunes the emptied source folders rather than leaving the husks.
    from qobuz_librarian.web import app as webapp
    from qobuz_librarian.web import flows
    captured = {}
    monkeypatch.setattr(flows, "execute_migration",
                        lambda j, chosen, dest, *, in_place, src: captured.update(
                            dest=dest, in_place=in_place, src=src))
    fn = webapp._resume_migration(None, {"dest": "/d", "in_place": True, "src": "/old"})
    fn(None, [])
    assert captured == {"dest": "/d", "in_place": True, "src": Path("/old")}


def test_album_tag_with_year_isnt_doubled():
    plan = m.build_plan([(Path("/s/x.flac"),
        _meta(album="Black Sands (2010)", year=2010, title="Kong", track=3), "tags")],
        Path("/dest"))
    assert plan.placed[0].dest_rel == Path("Artist/Black Sands (2010)/03 - Kong.flac")


def test_bare_year_album_title_keeps_its_name_with_year():
    plan = m.build_plan([(Path("/s/x.flac"),
        _meta(album="1989", year=2014, title="T", track=1), "tags")], Path("/dest"))
    assert plan.placed[0].dest_rel == Path("Artist/1989 (2014)/01 - T.flac")


def test_fingerprint_lookup_resolves_album_year_and_is_placeable():
    resp = {"results": [{"score": 0.98, "recordings": [{
        "title": "Kong", "artists": [{"name": "Bonobo"}],
        "releasegroups": [
            {"type": "Single", "title": "Kong", "releases": [{"date": {"year": 2009}}]},
            {"type": "Album", "title": "Black Sands", "artists": [{"name": "Bonobo"}],
             "releases": [{"date": {"year": 2011}}, {"date": {"year": 2010}}]},
        ]}]}]}
    meta = m.identify_from_lookup(resp, 0.9, "stem", ".flac")
    assert meta["album"] == "Black Sands"      # Album type preferred over Single
    assert meta["year"] == 2010                # earliest release year
    assert meta["albumartist"] == "Bonobo"
    assert m.is_placeable(meta)                # the whole point of F1: now placeable


def test_fingerprint_recording_without_album_stays_unplaceable():
    resp = {"results": [{"score": 0.99, "recordings": [{
        "title": "Mystery", "artists": [{"name": "X"}], "releasegroups": []}]}]}
    assert m.identify_from_lookup(resp, 0.9, "stem", ".flac") is None


def test_fingerprint_compilation_releasegroup_is_flagged():
    resp = {"results": [{"score": 0.97, "recordings": [{
        "title": "Hit", "artists": [{"name": "Some Artist"}],
        "releasegroups": [{"type": "Album", "secondarytypes": ["Compilation"],
                           "title": "Now 50", "artists": [{"name": "Various Artists"}],
                           "releases": [{"date": {"year": 2001}}]}]}]}]}
    meta = m.identify_from_lookup(resp, 0.9, "stem", ".flac")
    assert meta["compilation"] is True
