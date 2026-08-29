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

# Leading list junk: "1.", "1)", "-", "*", bullets, and stray quotes/brackets.
LEAD = re.compile(r"^[\s\d]*[\.\)\-\*•–—:]*\s*")
TRAIL = re.compile(r"[\s\-–—_,;:|]+$")
WRAP = re.compile(r"^[\[\(\{\'\"“‘\s]+|[\]\)\}\'\"”’\s]+$")


def scrub(line):
    """Strip list decoration and stray wrapping characters from a raw line."""
    text = line.strip()
    text = LEAD.sub("", text)
    text = WRAP.sub("", text)
    text = TRAIL.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


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


def best_match(entry, pool=5):
    query = " ".join(filter(None, (entry.get("artist"), entry["title"])))
    candidates = fetch.search(query, limit=pool)
    ranked = sorted(
        ((score(entry, c), c) for c in candidates), key=lambda pair: pair[0], reverse=True
    )
    if not ranked or ranked[0][0] <= 0:
        return 0.0, None
    return ranked[0]


def resolve_all(entries, pool=5, jobs=4, progress=None):
    """Search every entry concurrently. Returns [(entry, confidence, candidate)]."""
    results = [None] * len(entries)

    def work(index):
        entry = entries[index]
        try:
            confidence, candidate = best_match(entry, pool=pool)
        except Exception as exc:
            confidence, candidate = 0.0, None
            entry["error"] = str(exc)
        results[index] = (entry, confidence, candidate)
        if progress:
            progress(entry, confidence, candidate)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool_exec:
        list(pool_exec.map(work, range(len(entries))))
    return results
