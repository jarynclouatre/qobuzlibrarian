"""streamrip quality-tier constants and cap detection."""
import sys

from qobuz_librarian import config as cfg

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

# Streamrip quality tiers we'll actually receive after download.
# 1=320 MP3, 2=16/44.1, 3=24-bit up to 96kHz, 4=24-bit up to 192kHz.
_STREAMRIP_QUALITY_CAPS = {
    1: (16, 44100),
    2: (16, 44100),
    3: (24, 96000),
    4: (24, 192000),
}
_streamrip_cap_cache = None


def reset_streamrip_cap_cache():
    """Drop the cached cap so the next call re-derives it.

    Call this after STREAMRIP_QUALITY changes at runtime (the Settings
    page) — otherwise the upgrade scanner keeps reasoning at the quality
    that was in effect when the cap was first computed.
    """
    global _streamrip_cap_cache
    _streamrip_cap_cache = None


def streamrip_quality_cap():
    """Return (max_bit_depth, max_sample_rate_hz) we will actually receive
    given the user's quality setting. Cached after first call.

    rip.py always invokes streamrip with `-q cfg.STREAMRIP_QUALITY`, and the
    CLI flag overrides whatever streamrip's own config.toml says — so
    STREAMRIP_QUALITY (env / compose.yaml) is the authoritative value and
    the cap must be derived from it, not the config file. Reading the cap
    from config.toml instead made the upgrade scanner reason about a
    different quality than downloads actually use: bump STREAMRIP_QUALITY
    to 4 for hi-res and the scanner would still cap at the seeded config's
    value and never surface the hi-res upgrades it could now fetch. The
    config.toml is consulted only as a fallback when STREAMRIP_QUALITY
    isn't a recognised tier.
    """
    global _streamrip_cap_cache
    if _streamrip_cap_cache is not None:
        return _streamrip_cap_cache
    cap = (24, 192000)  # safe permissive default
    try:
        q = int(cfg.STREAMRIP_QUALITY)
    except (TypeError, ValueError):
        q = None
    if q in _STREAMRIP_QUALITY_CAPS:
        cap = _STREAMRIP_QUALITY_CAPS[q]
    else:
        try:
            with open(cfg.STREAMRIP_CONFIG, "rb") as f:
                config = tomllib.load(f)
            cq = (config.get("qobuz") or {}).get("quality")
            if isinstance(cq, int) and cq in _STREAMRIP_QUALITY_CAPS:
                cap = _STREAMRIP_QUALITY_CAPS[cq]
        except Exception:
            pass
    _streamrip_cap_cache = cap
    return cap


def format_quality(bits, rate):
    if not bits or not rate:
        return "?"
    return f"{bits}/{rate / 1000:g}"
