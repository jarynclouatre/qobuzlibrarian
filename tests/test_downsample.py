from qobuz_librarian.integrations.downsample_engine import _encode_opts_for_bps


def test_resample_preserves_source_bit_depth():
    base = "aresample=resampler=soxr"

    # 24-bit must stay 24-bit: feeding s32 to the flac encoder writes a 32-bit
    # stream unless bits_per_raw_sample pins it, which would inflate the master.
    af, fmt, depth = _encode_opts_for_bps(24, base)
    assert fmt == "s32"
    assert depth == ["-bits_per_raw_sample", "24"]
    assert af.endswith("aformat=sample_fmts=s32")

    # 16-bit stays 16-bit with no depth pin and the filter left alone.
    assert _encode_opts_for_bps(16, base) == (base, "s16", [])

    # Unknown depth (header unreadable) falls back to the safe s32 default.
    assert _encode_opts_for_bps(0, base) == (base, "s32", [])
