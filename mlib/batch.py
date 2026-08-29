"""Fetch a known list of songs. No model involved.

When the artist and title are already known there is no judgement left to make,
so this matches candidates with string similarity and duration sanity instead of
spending tokens. Anything it cannot match confidently is reported rather than
guessed at.
"""
import difflib
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor

from . import fetch
from .agent import MAX_SECONDS, MIN_SECONDS, REJECT

# Words that mean "not the studio track", unless the request asked for them.
VARIANT = re.compile(r"\b(live|cover|remix|karaoke|instrumental|acoustic|sped up|slowed|8d)\b", re.I)

# A line looks like:  Artist - Title            (optionally  ... :: Album)
LINE = re.compile(r"^\s*(?P<body>.+?)\s*(?:::\s*(?P<album>.+?))?\s*$")

# Chapter timestamps ("0:00 ", "1:02:33 - ") lead every line of a tracklist
# copied out of a video description.
TIMESTAMP = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s*[-–—.)\]]?\s*")

# List decoration: "1.", "12)", "-", "*", bullets. Only a number that is
# followed by a separator counts, so a title starting with a year survives.
LEAD = re.compile(r"^\s*(?:\d{1,3}\s*[\.\)\-–—:]\s+|[-*•–—]\s+)")

# Trailing junk. "!" and "?" stay - they are often part of the title.
TRAIL = re.compile(r"[\s\.,;:|_\-–—]+$")

WRAP_OPEN = re.compile(r"^[\[\(\{'\"“‘\s]+")
WRAP_CLOSE = re.compile(r"[\]\)\}'\"”’\s]+$")


def scrub(line):
    """Strip timestamps, list decoration and stray wrapping from a raw line."""
    text = line.strip()
    text = TIMESTAMP.sub("", text)
    text = LEAD.sub("", text)
    text = WRAP_OPEN.sub("", text)
    text = WRAP_CLOSE.sub("", text)
    text = TRAIL.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize(text):
    """Casefold and drop punctuation, but keep non-Latin scripts intact.

    Stripping to [a-z0-9] erases Cyrillic, Greek and CJK entirely, which made
    every such title normalize to "" and score meaninglessly.
    """
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def parse_list(text):
    """Parse a song list. Returns (entries, bad_lines).

    Accepts "Artist - Title", or a bare title with whatever decoration came with
    it. A bare title leaves artist as None, to be filled in later from the match.
    """
    entries, bad = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        body = scrub(m.group("body"))
        album = (m.group("album") or "").strip()
        if not body:
            bad.append(raw)
            continue

        parts = re.split(r"\s+[-–—]\s+", body, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            artist, title = parts[0].strip(), parts[1].strip()
        else:
            artist, title = None, body

        entries.append(
            {
                "artist": artist,
                "title": title,
                "album": album or "Singles",
                "raw": line,
            }
        )
    return entries, bad


def score(entry, candidate):
    """0..1 confidence that `candidate` is the requested track."""
    duration = candidate.get("duration") or 0
    if duration and not (MIN_SECONDS <= duration <= MAX_SECONDS):
        return 0.0
    cand_title = candidate.get("raw_title") or ""
    if REJECT.search(cand_title):
        return 0.0

    wanted = normalize(" ".join(filter(None, (entry.get("artist"), entry["title"]))))
    haystack = normalize(cand_title + " " + (candidate.get("channel") or ""))

    ratio = difflib.SequenceMatcher(None, wanted, haystack).ratio()

    # Require the title itself to be present; artist often lives in the channel.
    title_norm = normalize(entry["title"])
    if title_norm and title_norm in haystack:
        ratio = max(ratio, 0.72)
    artist_norm = normalize(entry.get("artist") or "")
    if artist_norm and artist_norm in haystack:
        ratio += 0.10

    # Penalise alternate versions unless the request asked for one.
    if VARIANT.search(cand_title) and not VARIANT.search(entry["raw"]):
        ratio -= 0.28

    return max(0.0, min(ratio, 1.0))


def best_match(entry, pool=5, keep=4):
    """Best candidate, plus runners-up to fall back on if a download fails.

    Sources go missing or turn out to be age-gated, so a single URL per song is
    not enough - keep the next best few and try them in turn.
    """
    query = " ".join(filter(None, (entry.get("artist"), entry["title"])))
    candidates = fetch.search(query, limit=pool)
    ranked = sorted(
        ((score(entry, c), c) for c in candidates), key=lambda pair: pair[0], reverse=True
    )
    ranked = [(sc, c) for sc, c in ranked if sc > 0]
    if not ranked:
        entry["_alternates"] = []
        return 0.0, None
    entry["_alternates"] = [c for _sc, c in ranked[1:keep]]
    return ranked[0]


def resolve_all(entries, pool=5, jobs=4, progress=None, keep=4):
    """Search every entry concurrently. Returns [(entry, confidence, candidate)]."""
    results = [None] * len(entries)

    def work(index):
        entry = entries[index]
        try:
            confidence, candidate = best_match(entry, pool=pool, keep=keep)
        except Exception as exc:
            confidence, candidate = 0.0, None
            entry["error"] = str(exc)
        results[index] = (entry, confidence, candidate)
        if progress:
            progress(entry, confidence, candidate)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool_exec:
        list(pool_exec.map(work, range(len(entries))))
    return results
