"""String normalization, path sanitization, and title-stripping helpers.

The regex constants, cache sizes, and iteration limits here are load-bearing:
changing them shifts fuzzy-matching results across the codebase.

normalize() and similarity() are used in:
  - find_album_dir_filesystem (fuzzy folder matching)
  - compute_missing (title matching)
  - filter_owned_albums (catalog dedup)
  - consolidation sibling detection

strip_edition_suffix() and strip_album_decorations() are used in:
  - compute_missing (matching "Foo (LP Version)" to Qobuz "Foo")
  - find_sibling_album_dirs (dedup by bare title)
  - filter_owned_albums (year-aware catalog filtering)
"""
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

# ── Path sanitization ─────────────────────────────────────────────────────────
_BEETS_BAD_CHARS_RE = re.compile(r'[\\/<>:"?*|\x00-\x1f]')
_NORMALIZE_RE       = re.compile(r"[^a-z0-9]+")
_WHITESPACE_RUN_RE  = re.compile(r"\s+")


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
    """Sanitize a string for use as a beets folder/file name component.

    Replaces chars beets rejects, strips leading dashes and trailing dots.
    """
    if not s:
        return ""
    s = _BEETS_BAD_CHARS_RE.sub("_", s)
    s = s.strip().rstrip(".")
    if s.startswith("-"):
        s = "_" + s[1:]
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
    genuinely different recording.

    Strips up to 3 trailing-paren groups (handles "Foo (Remaster) (Mono)").
    """
    if not title:
        return title
    s = title.strip()
    for _ in range(3):
        m = _TRAILING_PAREN_CAPTURE_RE.search(s)
        if not m:
            break
        inside = m.group(1).strip()
        # Keep this group if it's a performance-variant marker.
        if _PERFORMANCE_VARIANT_RE.search(inside):
            break
        new = s[: m.start()].strip()
        if not new:
            break
        s = new
    return s or title


# ── Album-name decoration stripping ──────────────────────────────────────────
_YEAR_PAREN_RE     = re.compile(r"\s*\([^)]*\d{4}[^)]*\)\s*$")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")
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

    Deliberately NOT stripped (these are distinct releases):
      'Cassadaga: A Companion'  (companion EP — different recordings)
      'Album: Live in Tokyo'    (live album)
      'Album: B-Sides'          (rarities)

    Iterates up to 8 times so combined decorations like
    'Foo: Deluxe Edition (2023)' fully strip in a single call.
    """
    s = name
    for _ in range(8):
        new = _LEADING_YEAR_RE.sub("", s).strip()
        if new == s:
            new = _YEAR_PAREN_RE.sub("", s).strip()
        if new == s:
            new = _TRAILING_PAREN_RE.sub("", s).strip()
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
