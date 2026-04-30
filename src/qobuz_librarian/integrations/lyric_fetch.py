"""Multi-provider lyrics fetcher.

The engine behind the import-time lyric hook and the library lyrics pass:
writes synced (or plain) lyrics into FLAC tags or .lrc sidecars.

  • Tries synced lyrics from each provider; rejects results whose timing
    doesn't fit the track length.
  • Falls back to plain lyrics across the providers; only writes plain
    lyrics when the track has no existing lyrics at all.
  • Per-run circuit breaker disables a provider after several consecutive
    connection-style failures.
  • State file tracks per-file status so subsequent passes only re-check
    tracks worth re-checking.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

# mutagen and syncedlyrics are decoupled on purpose: tag-only operations
# (classify/read/write embedded lyrics, .lrc sidecars) need only mutagen,
# while provider fetching needs syncedlyrics. Importing them together would
# null FLAC whenever the network lib is absent, silently disabling the
# mutagen-only paths too.
try:
    from mutagen.flac import FLAC
except Exception:  # mutagen missing — tag I/O unavailable
    FLAC = None  # type: ignore

try:
    import syncedlyrics
    AVAILABLE = FLAC is not None
    IMPORT_ERROR: Optional[Exception] = (
        None if AVAILABLE else ImportError("mutagen unavailable"))
except Exception as _e:  # missing deps shouldn't crash the ingest pipeline
    syncedlyrics = None  # type: ignore
    AVAILABLE = False
    IMPORT_ERROR = _e

# ── Defaults & tunables ──────────────────────────────────────────────────────
DEFAULT_PROVIDERS  = ["Lrclib", "NetEase", "Musixmatch"]
DEFAULT_STATE_FILE = Path(__file__).resolve().parent / ".lyric_fetch_state.json"

SYNCED_RE = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]")
LRC_TS_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")

RECHECK_AFTER_DAYS = 30
MAX_TRACK_SECONDS  = 60 * 20

# Duration-fit tolerances for synced lyrics. The lower-bound check is
# deliberately loose: songs with long instrumental intros/outros legitimately
# leave a large gap between the last lyric timestamp and the track end, and
# a partial LRC is still useful. We only reject when the LRC ends *past* the
# track or is so much shorter than the track that it's almost certainly the
# wrong song.
LRC_OVERRUN_GRACE  = 15     # last timestamp may exceed track length by this much
LRC_MIN_COVERAGE   = 0.2    # last timestamp should reach at least this fraction
LRC_GAP_GRACE      = 240    # …unless the gap to track end is within this many seconds

# Provider circuit breaker. With ≥8 workers transient errors compound quickly,
# so the threshold is bumped from 3 to give providers more rope before being
# disabled. After PROVIDER_COOLDOWN_SECONDS the provider is re-enabled and
# given another chance — important for overnight runs where a brief provider
# outage shouldn't disable that provider for hours.
PROVIDER_FAIL_THRESHOLD = 5
PROVIDER_COOLDOWN_SECONDS = 600  # 10 min before a disabled provider gets retried
_PROVIDER_ERROR_RE = re.compile(
    r"An error occurred|Connection refused|Max retries|Name or service not known|"
    r"timed out|TimeoutError|NewConnectionError|"
    r"\b429\b|Too Many Requests|rate[- ]?limit|"
    r"\b50[23]\b|Service Unavailable|Bad Gateway",
    re.IGNORECASE,
)
_dead_providers: dict[str, float] = {}   # provider -> epoch when disabled
_provider_fails: dict[str, int] = {}


def _is_provider_dead(prov: str, log: Optional[logging.Logger] = None) -> bool:
    """
    Return True if `prov` is currently in the cooldown window. If the cooldown
    has elapsed, clear the entry (and reset the strike count) so the next call
    actually queries the provider again. Caller must NOT hold _breaker_lock.
    """
    with _breaker_lock:
        disabled_at = _dead_providers.get(prov)
        if disabled_at is None:
            return False
        if time.time() - disabled_at >= PROVIDER_COOLDOWN_SECONDS:
            del _dead_providers[prov]
            _provider_fails[prov] = 0
            if log is not None:
                log.info("provider %s cooldown elapsed — re-enabling", prov)
            return False
        return True

# Locks for concurrent execution. _state_lock guards the shared state dict
# during save_state (iter races with mutation). _breaker_lock guards the
# circuit-breaker counters/sets that all worker threads share.
_state_lock = threading.Lock()
_breaker_lock = threading.Lock()


# ── Thread-safe provider error capture ───────────────────────────────────────
# syncedlyrics logs provider errors via Python's logging module. We capture
# warnings/errors per-thread so the circuit-breaker regex can see them, then
# silence the StreamHandlers syncedlyrics installs on each provider logger so
# they don't double-print to stderr during runs.
class _ChatterCapture(logging.Handler):
    """Buffers warning+ records into a thread-local list during begin/end."""
    _local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        buf = getattr(self._local, "buf", None)
        if buf is None:
            return
        try:
            buf.append(self.format(record))
        except Exception:
            pass

    @classmethod
    def begin(cls) -> None:
        cls._local.buf = []

    @classmethod
    def end(cls) -> str:
        buf = getattr(cls._local, "buf", None)
        cls._local.buf = None
        return "\n".join(buf or [])


_chatter_handler = _ChatterCapture(level=logging.WARNING)
_chatter_handler.setFormatter(logging.Formatter("%(message)s"))

if AVAILABLE:
    # Stop syncedlyrics' LRCProvider.__init__ from re-adding a StreamHandler
    # to each provider's named logger every time it's instantiated (which
    # happens once per syncedlyrics.search() call → handler list grows
    # unbounded and stderr fills with duplicate provider chatter).
    try:
        from syncedlyrics.providers.base import LRCProvider as _LRCProvider
        _orig_lrc_init = _LRCProvider.__init__

        def _quiet_lrc_init(self):  # type: ignore[no-redef]
            _orig_lrc_init(self)
            for h in list(self.logger.handlers):
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    self.logger.removeHandler(h)

        _LRCProvider.__init__ = _quiet_lrc_init  # type: ignore[assignment]
    except Exception:
        pass

    # Route the syncedlyrics + per-provider loggers through our thread-local
    # capture handler. Disable propagation so they don't escape to root.
    for _name in ("syncedlyrics", "Lrclib", "NetEase", "Megalobiz",
                  "Musixmatch", "Genius"):
        _lg = logging.getLogger(_name)
        for _h in list(_lg.handlers):
            if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
                _lg.removeHandler(_h)
        _lg.propagate = False
        _lg.addHandler(_chatter_handler)
        _lg.setLevel(logging.WARNING)


# ── State ────────────────────────────────────────────────────────────────────
@dataclass
class TrackState:
    mtime: float = 0.0
    size: int = 0
    status: str = ""        # synced | plain | not_found | error | skipped
    source: str = ""        # provider that succeeded
    attempts: int = 0
    last_seen: float = 0.0  # epoch of last attempt


def load_state(path: Path = DEFAULT_STATE_FILE) -> dict[str, TrackState]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        return {k: TrackState(**v) for k, v in raw.items()}
    except Exception as e:
        # L2: log corrupt state file instead of silently returning empty dict
        logging.getLogger("lyric_fetch").warning(
            "State file unreadable (%s), starting fresh: %s", path, e
        )
        return {}


def save_state(state: dict[str, TrackState], path: Path = DEFAULT_STATE_FILE) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {k: v.__dict__ for k, v in state.items()},
        indent=0, separators=(",", ":"),
    ))
    tmp.replace(path)


# ── Lyrics classification & tag I/O ──────────────────────────────────────────
def classify(text: Optional[str]) -> str:
    if not text or not text.strip():
        return "none"
    return "synced" if SYNCED_RE.search(text) else "plain"


def get_existing_lyrics(f) -> Optional[str]:
    for key in ("lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS"):
        if key in f.tags:
            v = f.tags[key]
            if v:
                return v[0]
    return None


def write_lyrics(f, content: str) -> None:
    # Vorbis comments are case-insensitive in mutagen, so assigning
    # "lyrics" already replaces any existing lyrics/LYRICS value — and
    # only the distinct `unsyncedlyrics` field needs explicit removal
    # (deleting it is likewise case-insensitive, so it also clears
    # UNSYNCEDLYRICS). The previous implementation deleted key "LYRICS"
    # *after* writing it which, being case-insensitive, wiped the lyrics
    # just written — embed/both silently stored nothing.
    if "unsyncedlyrics" in f.tags:
        del f.tags["unsyncedlyrics"]
    f.tags["lyrics"] = [content]
    f.save()


def write_sidecar(path: Path, content: str) -> None:
    """Write lyrics to a .lrc file next to the track (UTF-8)."""
    path.with_suffix(".lrc").write_text(content, encoding="utf-8")


def write_output(path: Path, f, content: str, fmt: str) -> None:
    """Persist lyrics per fmt: 'embed' (FLAC tag), 'sidecar' (.lrc), 'both'."""
    fmt = (fmt or "embed").strip().lower()
    if fmt not in ("embed", "sidecar", "both"):
        fmt = "embed"
    if fmt in ("embed", "both"):
        write_lyrics(f, content)
    if fmt in ("sidecar", "both"):
        write_sidecar(path, content)


# Title suffixes that confuse provider matching. Strip Spotify-style
# "(Remastered 2009)", "(Album Version)", "(Live at Wembley)", "[Mono]",
# trailing " - 2009 Remaster", etc., before querying — providers index the
# canonical title.
_TITLE_NOISE_KEYWORDS = (
    "remaster", "remastered", "remix", "remixed", "re-recorded", "rerecorded",
    "album version", "single version", "radio edit", "radio version",
    "extended version", "extended mix", "edit", "demo", "live",
    "acoustic", "instrumental", "mono", "stereo",
    "bonus track", "bonus", "deluxe", "explicit", "clean version",
    "alternate take", "alternate version", "anniversary",
    "expanded edition", "anniversary edition",
)
_kw_alt = "|".join(re.escape(k) for k in _TITLE_NOISE_KEYWORDS)
_TITLE_NOISE_RE = re.compile(
    rf"\s*\([^()]*(?:{_kw_alt})[^()]*\)|"
    rf"\s*\[[^\[\]]*(?:{_kw_alt})[^\[\]]*\]|"
    rf"\s+-\s+[^-]*(?:{_kw_alt})[^-]*$",
    re.IGNORECASE,
)
del _kw_alt


def _clean_title(title: str) -> str:
    cleaned = title
    for _ in range(4):
        prev = cleaned
        cleaned = _TITLE_NOISE_RE.sub("", cleaned).strip()
        if cleaned == prev:
            break
    return cleaned or title.strip()


def build_query(f) -> Optional[str]:
    title  = (f.tags.get("title")  or [""])[0].strip()
    artist = (f.tags.get("artist") or f.tags.get("albumartist") or [""])[0].strip()
    if not title or not artist:
        return None
    return f"{_clean_title(title)} {artist}"


# ── Duration sanity check for synced LRCs ────────────────────────────────────
def lrc_max_seconds(text: str) -> Optional[float]:
    matches = LRC_TS_RE.findall(text)
    if not matches:
        return None
    best = 0.0
    for mm, ss, frac in matches:
        v = int(mm) * 60 + int(ss)
        if frac:
            v += int(frac.ljust(3, "0")) / 1000.0
        if v > best:
            best = v
    return best


def lrc_duration_sane(lyrics: str, track_seconds: float) -> tuple[bool, str]:
    """Reject LRCs whose timing clearly doesn't fit the track length."""
    if not track_seconds or track_seconds <= 0:
        return True, ""
    last = lrc_max_seconds(lyrics)
    if last is None:
        return True, ""
    if last > track_seconds + LRC_OVERRUN_GRACE:
        return False, f"LRC ends {last:.0f}s past track end ({track_seconds:.0f}s)"
    gap = track_seconds - last
    if last < track_seconds * LRC_MIN_COVERAGE and gap > LRC_GAP_GRACE:
        return False, f"LRC ends at {last:.0f}s, track is {track_seconds:.0f}s"
    return True, ""


# ── Provider query (with circuit breaker) ────────────────────────────────────
def _query_provider(query: str, prov: str, log: logging.Logger, **kwargs) -> Optional[str]:
    """
    Wrap syncedlyrics.search() for a single provider. Provider errors come
    through Python's logging module; _ChatterCapture buffers them per-thread
    so the circuit-breaker regex can scan them. After PROVIDER_FAIL_THRESHOLD
    strikes the provider is skipped for the rest of this run; any successful
    result clears strikes.
    """
    if _is_provider_dead(prov, log):
        return None
    _ChatterCapture.begin()
    try:
        result = syncedlyrics.search(query, providers=[prov], **kwargs)
    except Exception as e:
        log.debug("provider %s raised: %s", prov, e)
        result = None
    chatter = _ChatterCapture.end()
    if _PROVIDER_ERROR_RE.search(chatter):
        with _breaker_lock:
            n = _provider_fails.get(prov, 0) + 1
            _provider_fails[prov] = n
            first_line = chatter.splitlines()[0] if chatter else ""
            log.debug("provider %s soft-fail #%d: %s", prov, n, first_line)
            if n >= PROVIDER_FAIL_THRESHOLD:
                _dead_providers[prov] = time.time()
                log.warning("disabling provider %s for %ds after %d consecutive "
                            "failures (will retry after cooldown)",
                            prov, PROVIDER_COOLDOWN_SECONDS, n)
        return None
    if result:
        with _breaker_lock:
            _provider_fails[prov] = 0
    elif not _PROVIDER_ERROR_RE.search(chatter):
        # L4: Clean "not found" — not a connection failure, reset any stale fail count.
        with _breaker_lock:
            _provider_fails[prov] = 0
    return result


def search_lyrics(
    query: str, providers: list[str], duration: float, log: logging.Logger,
    skip_plain: bool = False,
) -> tuple[Optional[str], Optional[str], str, int]:
    """
    Returns (lyrics, provider_name, kind, providers_tried) where kind is
    'synced' or 'plain' and providers_tried counts queries actually attempted
    (skipping providers already disabled by the circuit breaker). The caller
    uses providers_tried==0 to distinguish 'no provider has it' from 'no
    provider was reachable', so a breaker-tripped run doesn't poison state.
    """
    tried = 0
    for prov in providers:
        if _is_provider_dead(prov, log):
            continue
        tried += 1
        result = _query_provider(query, prov, log, synced_only=True)
        if result and SYNCED_RE.search(result):
            ok, reason = lrc_duration_sane(result, duration)
            if not ok:
                log.info("rejected %s synced result: %s", prov, reason)
                continue
            return result, prov, "synced", tried
    if skip_plain:
        return None, None, "", tried
    for prov in providers:
        if _is_provider_dead(prov, log):
            continue
        tried += 1
        result = _query_provider(query, prov, log, plain_only=True)
        if result and result.strip():
            return result, prov, "plain", tried
    return None, None, "", tried


# ── Per-file processing & state-aware filter ─────────────────────────────────
def should_process(
    path: Path,
    st: Optional[TrackState],
    rescan: bool,
    *,
    mtime: Optional[float] = None,
    size: Optional[int] = None,
    skip_existing_plain: bool = False,
) -> bool:
    """
    Decide whether `path` is worth (re-)processing. mtime/size may be passed
    in by the caller (e.g. from a bulk listing) to avoid an extra path.stat().
    """
    if rescan or st is None:
        return True
    if mtime is None or size is None:
        try:
            stat = path.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except OSError:
            return False
    if int(mtime) != int(st.mtime) or size != st.size:
        return True
    if st.status == "synced":
        return False
    if st.status == "plain":
        # --skip-plain caller: treat existing plain as final, don't try to
        # upgrade. Otherwise the default is to re-try every run hoping a
        # provider has gained synced lyrics for the track.
        return not skip_existing_plain
    if st.status in ("not_found", "error", "skipped"):
        # L1: "skipped" (long-track, missing-tags) now expires like not_found
        # instead of being re-opened on every run.
        age_days = (time.time() - st.last_seen) / 86400
        return age_days >= RECHECK_AFTER_DAYS
    return True


def _commit(state: dict[str, TrackState], key: str, st: TrackState) -> None:
    with _state_lock:
        state[key] = st


def process_file(
    path: Path, state: dict[str, TrackState],
    providers: list[str], dry_run: bool, log: logging.Logger,
    synced_only: bool = False,
    skip_existing_plain: bool = False,
    lyrics_format: str = "embed",
) -> str:
    key = str(path)
    st  = state.get(key) or TrackState()
    # TrackState is mutated below; if `key` was already present we'd be
    # mutating the same instance another worker (re-)scheduled would see.
    # Each path is owned by exactly one worker, so this is safe: only
    # commits to the shared dict are serialised (via _state_lock).

    try:
        f = FLAC(path)
    except Exception as e:
        log.error("FLAC open failed: %s — %s", path, e)
        st.status = "error"
        st.last_seen = time.time()
        st.attempts += 1
        _commit(state, key, st)
        return "error"

    stat = path.stat()
    st.mtime = stat.st_mtime
    st.size  = stat.st_size

    duration = getattr(f.info, "length", 0) or 0
    if duration > MAX_TRACK_SECONDS:
        st.status = "skipped"
        st.source = "long-track"
        st.last_seen = time.time()
        _commit(state, key, st)
        return "skipped-long"

    existing      = get_existing_lyrics(f)
    existing_kind = classify(existing)

    if existing_kind == "synced":
        st.status = "synced"
        st.last_seen = time.time()
        _commit(state, key, st)
        return "already-synced"

    if existing_kind == "plain" and skip_existing_plain:
        # --skip-plain: file already has plain lyrics; user opted out of
        # the upgrade pass. Refresh state so should_process keeps skipping it.
        st.status = "plain"
        st.last_seen = time.time()
        _commit(state, key, st)
        return "already-plain"

    query = build_query(f)
    if not query:
        st.status = "skipped"
        st.source = "missing-tags"
        st.last_seen = time.time()
        _commit(state, key, st)
        return "skipped-tags"

    # If the file already has plain lyrics, the plain-fallback pass is pure
    # waste: any plain result we'd find gets thrown away in the
    # `kept-existing-plain` branch below. Same shortcut applies when the
    # caller asked for synced-only.
    skip_plain = synced_only or existing_kind == "plain"
    lyrics, source, kind, providers_tried = search_lyrics(
        query, providers, duration, log, skip_plain=skip_plain,
    )
    st.attempts += 1
    st.last_seen = time.time()

    if not lyrics:
        if providers_tried == 0:
            # The circuit breaker had killed every provider before this
            # file's turn — we never actually asked anyone. Don't write
            # not_found (which would block re-checking for
            # RECHECK_AFTER_DAYS); commit a transient status that
            # should_process re-checks on the next run.
            st.status = "transient"
            st.source = "providers-unavailable"
            _commit(state, key, st)
            return "providers-unavailable"
        if existing_kind == "plain":
            # No synced available, but the file already has plain lyrics.
            # Don't regress to not_found — keep status=plain so future runs
            # keep trying to upgrade.
            st.status = "plain"
            st.source = "kept-existing"
            _commit(state, key, st)
            return "kept-existing-plain"
        st.status = "not_found"
        st.source = ""
        _commit(state, key, st)
        return "not-found"

    if kind == "synced":
        action = "wrote-synced"
    elif existing_kind == "none":
        action = "wrote-plain"
    else:
        st.status = "plain"
        st.source = "kept-existing"
        _commit(state, key, st)
        return "kept-existing-plain"

    if dry_run:
        st.status = kind
        st.source = f"{source} (dry-run)"
        _commit(state, key, st)
        return f"dry:{action}"

    try:
        write_output(path, f, lyrics, lyrics_format)
    except Exception as e:
        log.error("write failed: %s — %s", path, e)
        st.status = "error"
        _commit(state, key, st)
        return "write-error"

    st.status = kind
    st.source = source
    stat = path.stat()
    st.mtime = stat.st_mtime
    st.size = stat.st_size
    _commit(state, key, st)
    return action


# ── High-level entry point ───────────────────────────────────────────────────
def fetch_for_paths(
    paths: Iterable[Path],
    providers: Optional[list[str]] = None,
    delay: float = 0.0,
    state_path: Path = DEFAULT_STATE_FILE,
    dry_run: bool = False,
    rescan: bool = False,
    log: Optional[logging.Logger] = None,
    save_every: int = 25,
    should_stop: Optional[Callable[[], bool]] = None,
    workers: int = 8,
    synced_only: bool = False,
    skip_existing_plain: bool = False,
    lyrics_format: str = "embed",
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Counter:
    """
    Run the lyrics pipeline over `paths`. Returns Counter of outcome → n.
    State is loaded from / saved to `state_path` so re-runs only re-check
    tracks whose previous outcome warrants it. The per-run provider circuit
    breaker is reset on each call.

    `should_stop`, if supplied, is polled before each file and lets the
    caller stop cleanly (e.g. on SIGINT).

    `workers` controls per-file concurrency. Each file's work is dominated
    by network I/O (multi-provider HTTP), which releases the GIL — so
    threading is the right tool here. Default 8 is a reasonable balance
    against provider rate limits; raise it if your library is huge and
    your providers tolerate more parallelism, lower it (or set 1) to debug.
    """
    if log is None:
        log = logging.getLogger("lyric_fetch")
    if not AVAILABLE:
        log.warning("lyric_fetch unavailable: %s", IMPORT_ERROR)
        return Counter({"unavailable": 0})

    providers = providers or list(DEFAULT_PROVIDERS)
    with _breaker_lock:
        _dead_providers.clear()
        _provider_fails.clear()

    state = load_state(state_path)
    candidates = [Path(p) for p in paths
                  if should_process(Path(p), state.get(str(p)), rescan,
                                    skip_existing_plain=skip_existing_plain)]

    counts: Counter = Counter()
    total = len(candidates)
    workers = max(1, int(workers))

    def run_one(fp: Path) -> str:
        try:
            outcome = process_file(
                fp, state, providers, dry_run, log,
                synced_only=synced_only,
                skip_existing_plain=skip_existing_plain,
                lyrics_format=lyrics_format,
            )
        except Exception as e:
            log.exception("unexpected error on %s: %s", fp, e)
            outcome = "exception"
        if delay > 0 and outcome.startswith(("wrote-", "dry:", "not-found", "kept-existing")):
            time.sleep(delay)
        return outcome

    def checkpoint() -> None:
        try:
            with _state_lock:
                save_state(state, state_path)
        except Exception as e:
            # An overnight run shouldn't die because the disk hiccupped on
            # one checkpoint write. Log and keep going — the next checkpoint
            # (or the final one) will retry.
            log.warning("checkpoint failed (continuing): %s", e)

    completed = 0
    if workers == 1:
        for fp in candidates:
            if should_stop and should_stop():
                break
            outcome = run_one(fp)
            completed += 1
            counts[outcome] += 1
            log.info("[%d/%d] %s — %s", completed, total, outcome, fp.name)
            if progress_cb:
                progress_cb(completed, total, fp.name)
            if completed % save_every == 0:
                checkpoint()
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lyrics") as ex:
            futures = {ex.submit(run_one, fp): fp for fp in candidates}
            try:
                for fut in as_completed(futures):
                    fp = futures[fut]
                    try:
                        outcome = fut.result()
                    except Exception as e:
                        log.exception("worker raised on %s: %s", fp, e)
                        outcome = "exception"
                    # Defensive: nothing in this block *should* raise, but a
                    # logging error or filesystem blip during checkpoint
                    # shouldn't tear down a multi-hour run. Catch and continue.
                    try:
                        completed += 1
                        counts[outcome] += 1
                        log.info("[%d/%d] %s — %s", completed, total, outcome, fp.name)
                        if progress_cb:
                            progress_cb(completed, total, fp.name)
                        if completed % save_every == 0:
                            checkpoint()
                        if should_stop and should_stop():
                            for f in futures:
                                f.cancel()
                            break
                    except Exception as e:
                        log.exception("post-process error on %s: %s", fp, e)
            except KeyboardInterrupt:
                for f in futures:
                    f.cancel()
                raise

    checkpoint()
    return counts


# ── Scan-only indexer ────────────────────────────────────────────────────────
def index_existing(
    items: Iterable,
    *,
    state_path: Path = DEFAULT_STATE_FILE,
    log: Optional[logging.Logger] = None,
    workers: int = 64,
    save_every: int = 500,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Counter:
    """
    Fast scan-only pass — open each FLAC, classify the existing lyrics tag,
    and write state entries for files that already have synced or plain
    lyrics. No provider calls, so workers can be cranked far higher than the
    network path tolerates. Use this to seed the state file after a
    fresh import so a subsequent normal run skips already-synced files
    instead of re-checking them.

    `items` is an iterable of either `Path` or `(Path, mtime, size)` tuples.
    When mtime/size are provided (>0), they replace a per-file `stat()` call.
    Files for which `state` already records a matching mtime+size are
    skipped without opening the FLAC, so re-running --index after adding
    new files is cheap.
    """
    if log is None:
        log = logging.getLogger("lyric_fetch")
    if not AVAILABLE:
        log.warning("lyric_fetch unavailable: %s", IMPORT_ERROR)
        return Counter({"unavailable": 0})

    normalized: list[tuple[Path, float, int]] = []
    for it in items:
        if isinstance(it, tuple):
            p, mt, sz = it
            normalized.append((Path(p), float(mt or 0), int(sz or 0)))
        else:
            normalized.append((Path(it), 0.0, 0))

    state = load_state(state_path)
    counts: Counter = Counter()
    total = len(normalized)
    workers = max(1, int(workers))

    def index_one(fp: Path, mt_hint: float, sz_hint: int) -> str:
        key = str(fp)
        if mt_hint > 0 and sz_hint > 0:
            mtime, size = mt_hint, sz_hint
        else:
            try:
                stat = fp.stat()
                mtime, size = stat.st_mtime, stat.st_size
            except OSError as e:
                log.debug("stat failed: %s — %s", fp, e)
                return "stat-error"
        cached = state.get(key)
        if (cached is not None
                and int(mtime) == int(cached.mtime)
                and size == cached.size
                and cached.status in ("synced", "plain", "not_found", "skipped")):
            return f"cached-{cached.status}"
        try:
            f = FLAC(fp)
        except Exception as e:
            log.debug("FLAC open failed: %s — %s", fp, e)
            return "open-error"
        kind = classify(get_existing_lyrics(f))
        if kind == "none":
            # Don't write state for files with no lyrics — let the normal
            # run pick them up via should_process(st=None).
            return "no-lyrics"
        st = TrackState(
            mtime=mtime, size=size,
            status=kind, source="indexed",
            attempts=0, last_seen=time.time(),
        )
        _commit(state, key, st)
        return f"indexed-{kind}"

    def checkpoint() -> None:
        try:
            with _state_lock:
                save_state(state, state_path)
        except Exception as e:
            log.warning("checkpoint failed (continuing): %s", e)

    log.info("indexing %d files with %d workers (no provider calls)",
             total, workers)
    # Disk save is ~1MB JSON dump → expensive; progress log is cheap. Keep
    # them on separate cadences so the user sees movement at workers=32 but
    # we don't write the state file every few hundred ms.
    progress_every = max(1, min(250, total // 50 or 1))
    completed = 0
    last_log = time.monotonic()
    if workers == 1:
        for fp, mt, sz in normalized:
            if should_stop and should_stop():
                break
            outcome = index_one(fp, mt, sz)
            completed += 1
            counts[outcome] += 1
            if completed % progress_every == 0:
                now = time.monotonic()
                rate = progress_every / max(0.001, now - last_log)
                last_log = now
                log.info("[%d/%d] %.0f files/s — %s",
                         completed, total, rate, dict(counts))
            if completed % save_every == 0:
                checkpoint()
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lyrics-idx") as ex:
            futures = {ex.submit(index_one, fp, mt, sz): fp
                       for fp, mt, sz in normalized}
            try:
                for fut in as_completed(futures):
                    fp = futures[fut]
                    try:
                        outcome = fut.result()
                    except Exception as e:
                        log.debug("worker raised on %s: %s", fp, e)
                        outcome = "exception"
                    completed += 1
                    counts[outcome] += 1
                    if completed % progress_every == 0:
                        now = time.monotonic()
                        rate = progress_every / max(0.001, now - last_log)
                        last_log = now
                        log.info("[%d/%d] %.0f files/s — %s",
                                 completed, total, rate, dict(counts))
                    if completed % save_every == 0:
                        checkpoint()
                    if should_stop and should_stop():
                        for f in futures:
                            f.cancel()
                        break
            except KeyboardInterrupt:
                for f in futures:
                    f.cancel()
                raise

    checkpoint()
    return counts
