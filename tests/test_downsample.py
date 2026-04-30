from qobuz_librarian.library import downsample as ds


def test_scan_groups_hires_tracks_and_estimates_saving(monkeypatch, tmp_path):
    album = tmp_path / "Boards of Canada" / "Geogaddi (2002)"
    album.mkdir(parents=True)
    monkeypatch.setattr(ds, "HAVE_DOWNSAMPLE", True)
    monkeypatch.setattr(ds, "list_artist_album_dirs", lambda _d: [album])
    # Two hi-res tracks alongside one already-CD-rate track (n_flac == 3): the
    # candidate counts all three but only the two high-rate files get shrunk.
    # audio_size excludes the 20 MB of metadata/art per file, so the estimate
    # scales 80 MB of audio, not the full 100 MB.
    monkeypatch.setattr(ds, "scan_dir_for_hires", lambda _d: {
        "hires": [
            {"path": str(album / "01.flac"), "sr": 96000, "target": 48000,
             "size": 100_000_000, "audio_size": 80_000_000},
            {"path": str(album / "02.flac"), "sr": 192000, "target": 48000,
             "size": 100_000_000, "audio_size": 80_000_000},
        ],
        "n_flac": 3,
    })

    cands = ds.scan_artist_for_downsample(tmp_path / "Boards of Canada")
    assert len(cands) == 1
    c = cands[0]
    assert (c.artist, c.n_hires, c.n_flac) == ("Boards of Canada", 2, 3)
    assert c.source_rates == [96000, 192000]
    assert c.target_rates == [48000]
    # 96→48 sheds half the audio, 192→48 three-quarters — of the 80 MB audio.
    assert c.est_saving == 40_000_000 + 60_000_000
    assert "96kHz/192kHz → 48kHz" in c.detail
    assert "2/3 tracks" in c.detail
