"""streamrip quality-tier constants and cap detection."""
from qobuz_librarian import config as cfg

# Highest (bit_depth, sample_rate_hz) streamrip delivers per quality tier:
# 1=320 MP3, 2=16/44.1, 3=24-bit up to 96kHz, 4=24-bit up to 192kHz.
_STREAMRIP_QUALITY_CAPS = {
    1: (16, 44100),
    2: (16, 44100),
    3: (24, 96000),
    4: (24, 192000),
}


def streamrip_quality_cap():
    """Return (max_bit_depth, max_sample_rate_hz) streamrip will actually
    deliver at the current quality setting.

    rip.py invokes streamrip with `-q cfg.STREAMRIP_QUALITY`, and that flag
    overrides streamrip's own config — so STREAMRIP_QUALITY (env / Settings)
    is authoritative. config validates it to a 1-4 tier at load and the
    Settings page only accepts those values, so this is a live tier lookup:
    reading it fresh each call means a quality change takes effect at once.
    """
    return _STREAMRIP_QUALITY_CAPS[cfg.STREAMRIP_QUALITY]


def downsample_target_rate(sr_hz):
    """Sample rate (Hz) a file ends up at after the downsample hook runs.

    Mirrors the resample families in scripts/compress.py: the high-rate
    members of each integer-ratio family collapse to their 44.1/48 kHz base,
    and rates already at (or below) the base pass through unchanged. Unlike
    the script's target_rate, this returns the input for the no-change case
    rather than None, so callers can chain it without a guard.
    """
    if sr_hz in (88200, 176400, 352800):
        return 44100
    if sr_hz in (96000, 192000, 384000):
        return 48000
    return sr_hz


def format_quality(bits, rate):
    if not bits or not rate:
        return "?"
    return f"{bits}/{rate / 1000:g}"
