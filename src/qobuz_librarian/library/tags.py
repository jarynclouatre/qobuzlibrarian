"""String normalization, path sanitization, and title-stripping helpers.

The regex constants, cache sizes, and iteration limits here are load-bearing:
changing them shifts fuzzy-matching results across catalog matching, folder
resolution, and consolidation.
"""
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

# ── Path sanitization ─────────────────────────────────────────────────────────
_NORMALIZE_RE       = re.compile(r"[^a-z0-9]+")
_WHITESPACE_RUN_RE  = re.compile(r"\s+")

# beets' own default `replace` rules (its config_default.yaml), in the order it
# applies them. Running a name through these gives the exact folder/file beets
# would write — predicted_album_paths and the migrate placement both depend on
# that equivalence, so a name like "...And Justice for All" resolves instead of
# looking missing.
_BEETS_REPLACEMENTS = (
    (re.compile(r'[<>:?*|]'), "_"),
    (re.compile(r'"'),        "_"),
    (re.compile(r"[\\/]"),    "_"),
    (re.compile(r"^\."),      "_"),
    (re.compile(r"\.$"),      "_"),
    (re.compile(r"[\x00-\x1f]"), "_"),
    (re.compile(r"^-"),       "_"),
    (re.compile(r"\s+$"),     ""),
    (re.compile(r"^\s+"),     ""),
)


def clean_qobuz_string(s):
    """Trim surrounding whitespace, collapse internal whitespace runs, and
    strip a single pair of matching outer quotes from a Qobuz response field.

    Qobuz titles and artist names occasionally arrive with trailing spaces
    (e.g. ``"Hunky Dory "``) or wrapped in literal quotes (e.g. ``'"Heroes"'``).
    Leaving those in place produces folder names like ``Hunky Dory  (1971)/``
    or ``_Heroes_ (1977)/`` after beets sanitizes the quotes. Normalising at
    the API response boundary means downstream consumers (process, queue,
    web, beets) all get the clean form for free.

    Returns the empty string for None or non-string input so callers can rely
    on a usable str. Matching outer quotes are stripped only when both sides
    of the string carry them; quoted text inside a longer string is preserved
    (``the "wall" album`` is left alone).
    """
    if not isinstance(s, str):
        return ""
    out = _WHITESPACE_RUN_RE.sub(" ", s).strip()
    if len(out) >= 2:
        first, last = out[0], out[-1]
        if (first == '"' and last == '"') or (first == "'" and last == "'"):
            out = out[1:-1].strip()
    return out

# Normalized forms of the "Various Artists" placeholder used by Qobuz and
# many libraries. Matched after normalize() (lowercased, alphanum-only)
# so "Various Artists", "various artists", "VA", "various", "V.A." all
# fall into the same bucket. Used as a guard wherever Qobuz can't return
# a meaningful artist catalog for a compilation alias.
VA_NORMALIZED = frozenset({"variousartists", "various", "va"})


@lru_cache(maxsize=2048)
def beets_sanitize(s):
    """Sanitize a string into the path component beets would write for it.

    A leading or trailing dot becomes ``_`` (not dropped) and a leading dash
    becomes ``_``, matching beets exactly — anything less mis-predicts the
    on-disk folder for names like "...And Justice for All".
    """
    if not s:
        return ""
    for rx, repl in _BEETS_REPLACEMENTS:
        s = rx.sub(repl, s)
    return s


@lru_cache(maxsize=8192)
def normalize(s):
    """ASCII-fold and strip punctuation for fuzzy comparison.

    Pure CJK / emoji titles that strip to "" return "" — callers treat
    that as "can't compare" (similarity() returns 0.0 for such pairs).
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    return _NORMALIZE_RE.sub("", s.lower())


@lru_cache(maxsize=8192)
def similarity(a, b):
    """Normalized similarity score [0.0, 1.0] between two strings.

    Two empty-normalized strings (e.g. pure-CJK titles) would score 1.0
    against each other — a false-positive waiting to bite
    find_qobuz_album_for_dir. Force 0.0 in that case.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


# ── Track-title edition stripping ─────────────────────────────────────────────
# Performance variants mark genuinely different recordings and must NOT be
# stripped. If a parenthesized suffix contains one of these, we leave it
# attached so the title remains distinct in compute_missing.
_PERFORMANCE_VARIANT_RE = re.compile(
    r"\b(?:"
    r"acoustic|live|demo|remix|instrumental|"
    r"a\s*cappella|acapella|"
    r"cover|reprise|interlude|skit|"
    r"radio\s*edit|extended\s*mix|club\s*mix|"
    r"dub(?:\s*mix)?|piano\s*version|"
    r"unplugged|orchestral|symphonic|"
    r"feat\.?|featuring|with\s+\w+|"
    r"karaoke|backing\s*track|"
    r"alternate\s*(?:take|version|mix)|"
    r"early\s*(?:take|version|mix)|"
    r"rough\s*(?:take|mix|cut)|"
    r"session"
    r")\b",
    re.IGNORECASE,
)

# Trailing parenthesized chunk capturer — non-greedy, anchored to end.
_TRAILING_PAREN_CAPTURE_RE = re.compile(r"\s*\(([^()]*)\)\s*$")


@lru_cache(maxsize=4096)
def strip_edition_suffix(title):
    """Strip trailing parenthesized edition tags from a track title.

    Strips: anything in trailing parens that does NOT contain a performance-
    variant keyword. So (LP Version), (2014 Remaster), (Deluxe Edition),
    (Bonus Track), (Mono), (Explicit), (Japanese Version), (Edit), and
    arbitrary other release-version markers all get removed.

    Preserves: (Acoustic), (Live), (Demo), (Remix), (Instrumental),
    (Radio Edit), (Extended Mix), (feat. X), and other markers indicating a
    genuinely different recording — including when an edition tag sits OUTSIDE
    a performance one, so "Song (LP Version) (Remix)" → "Song (Remix)".

    Handles up to 4 trailing-paren groups ("Foo (Remaster) (Mono)").
    """
    if not title:
        return title
    s = title.strip()
    kept = []
    for _ in range(4):
        m = _TRAILING_PAREN_CAPTURE_RE.search(s)
        if not m:
            break
        head = s[: m.start()].strip()
        if not head:
            break
        if _PERFORMANCE_VARIANT_RE.search(m.group(1)):
            kept.append(m.group(1).strip())
        s = head
    for group in reversed(kept):
        s = f"{s} ({group})"
    return s or title


def strip_trailing_parens(title):
    """A title with every trailing parenthesized group removed — both edition
    AND performance-variant tags. Distinct from strip_edition_suffix (which
    keeps performance variants): this only answers "is there a non-empty core
    once the parenthesised decorations are gone", which catalog matching uses to
    tell a non-Latin title apart from its ASCII-folded edition tag."""
    s = (title or "").strip()
    while True:
        m = _TRAILING_PAREN_CAPTURE_RE.search(s)
        if not m:
            return s
        head = s[: m.start()].strip()
        if not head:
            return s
        s = head


# ── Album-name decoration stripping ──────────────────────────────────────────
_YEAR_PAREN_RE = re.compile(r"\s*\([^)]*\d{4}[^)]*\)\s*$")
# Leading-year forms from alternate beets path templates, e.g.
# `[$year] $album/` produces "[1971] Hunky Dory"; `$year - $album/`
# produces "1971 - Hunky Dory". A bare year requires a dash separator so a
# title that simply IS a year ("1989", "2112 (Deluxe)") isn't mistaken for a
# year prefix and eaten; the bracketed form is unambiguous.
_LEADING_YEAR_RE   = re.compile(
    r"^\s*(?:\[\s*\d{4}\s*\]\s*[-–—]?|\d{4}\s*[-–—])\s+")

# Edition keywords to strip from album title suffixes.
# Deliberately excluded (different products, stay separate):
#   companion / companion ep, live, demos, acoustic, instrumental,
#   remix / remixed, b-sides / b sides / rarities
_EDITION_TAIL_KEYWORDS = (
    r"(?:\d{4}\s+)?(?:re)?master(?:ed)?(?:\s+edition)?|"
    r"(?:\d{1,4}\w{0,2}\s+)?anniversary(?:\s+edition)?|"
    r"deluxe(?:\s+edition)?|"
    r"super\s+deluxe(?:\s+edition)?|"
    r"expanded(?:\s+edition)?|"
    r"special\s+edition|"
    r"collector'?s\s+edition|"
    r"limited\s+edition|"
    r"bonus\s+edition|"
    r"reissue"
)
_EDITION_TAIL_RE = re.compile(
    r"\s*[-–—:]\s*(?:" + _EDITION_TAIL_KEYWORDS + r")\s*$",
    re.IGNORECASE,
)

# The same distinct-release markers, applied to the parenthesized form: a
# trailing '(Live)' / '(Acoustic)' / '(Demos)' is a different record, not an
# edition, so it's never stripped — mirroring the colon/dash exclusions above
# and strip_edition_suffix on track titles. Without this a live or acoustic
# album collapses onto the studio one and gets hidden from the missing scan.
_ALBUM_VARIANT_RE = re.compile(
    r"\b(?:live|unplugged|acoustic|demos?|instrumental|"
    r"remix(?:ed|es)?|b[\s-]?sides?|rarities|companion|sessions?)\b",
    re.IGNORECASE,
)


def _strip_trailing_paren_tag(s):
    """Drop a trailing parenthesized edition tag, unless its contents mark a
    distinct release (live/acoustic/remix/...), which stays attached."""
    m = _TRAILING_PAREN_CAPTURE_RE.search(s)
    if not m or _ALBUM_VARIANT_RE.search(m.group(1)):
        return s
    return s[: m.start()].strip()


# Normalized forms of the markers above, for callers comparing already-
# normalized bare titles where normalize() has dropped the spaces that
# _ALBUM_VARIANT_RE's word boundaries rely on. Keep in step with it.
_ALBUM_VARIANT_TOKENS = (
    "live", "unplugged", "acoustic", "demos", "demo", "instrumental",
    "remixes", "remixed", "remix", "bsides", "rarities", "companion",
    "sessions", "session",
)


def differs_by_album_variant(shorter, longer):
    """True when normalized bare title ``longer`` is ``shorter`` plus a trailing
    distinct-release marker (live/acoustic/remix/...).

    Lets a prefix-based owned check tell 'Album' from 'Album (Live)' once both
    have been normalized to bare alphanumerics, so a live or acoustic record
    isn't mistaken for an un-stripped edition of the studio album.
    """
    if not longer.startswith(shorter):
        return False
    suffix = longer[len(shorter):]
    return any(suffix.startswith(tok) for tok in _ALBUM_VARIANT_TOKENS)


@lru_cache(maxsize=2048)
def strip_album_decorations(name):
    """Strip trailing edition decorations iteratively from an album name.

    Parenthesized decorations:
      'Revolver (2009 Remaster)' → 'Revolver'
      'Album (Deluxe) (2018)'    → 'Album'

    Colon / dash suffixes:
      'Cassadaga: Deluxe Edition' → 'Cassadaga'
      'Revolver - 2022 Remaster'  → 'Revolver'
      'Album: 50th Anniversary Edition' → 'Album'

    Deliberately NOT stripped (these are distinct releases), in either the
    parenthesized or colon/dash form:
      'Cassadaga: A Companion'  (companion EP — different recordings)
      'Album: Live in Tokyo' / 'Album (Live)'   (live album)
      'Album: B-Sides' / 'Greatest Hits (Acoustic)'  (rarities / acoustic set)

    Iterates up to 8 times so combined decorations like
    'Foo: Deluxe Edition (2023)' fully strip in a single call.
    """
    s = name
    for _ in range(8):
        new = _LEADING_YEAR_RE.sub("", s).strip()
        if new == s:
            new = _strip_trailing_paren_tag(s)
        if new == s:
            new = _EDITION_TAIL_RE.sub("", s).strip()
        if new == s or not new:
            break
        s = new
    return s or name


def strip_year_decoration(name):
    """Remove only a leading or trailing year tag from an album folder name.

    'Black Sands (2010)' and '[2010] Black Sands' both reduce to 'Black Sands',
    but edition/live/remaster tags are left attached — so 'Album (Live)' stays
    distinct from 'Album (2018)' and the two are never treated as one album.
    """
    s = _LEADING_YEAR_RE.sub("", name).strip()
    s = _YEAR_PAREN_RE.sub("", s).strip()
    return s or name
