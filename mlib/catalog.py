"""The song catalog - which songs the library should hold, apart from where the audio came from.

A song is a work ("Катюша"). It has up to two versions, vocal and instrumental,
and each version has sources: recordings found somewhere (YouTube, sovmusic.ru,
archive.org - see providers.py), each with its own performer and a status. The library file is just the current
source of one version, so a version can be re-sourced when the audio is bad or
the upload dies, and a source that was rejected once is never picked again.

    songs[<theme>/<slug>] = {title, theme, aliases, notes, versions}
    versions[vocal|instrumental] = {collection, album, current, library_path,
                                    status, sources}
    sources = [{id, provider, url, performer, title, status, reason, added,
                library_path, year?, flags?}]

Stored as catalog/songs.json - human-editable, keys sorted for stable diffs.
"""
import difflib
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from . import providers
from .config import REPO_ROOT
from .lock import file_lock
from .naming import clean, safe

CATALOG = REPO_ROOT / "catalog" / "songs.json"
VERSIONS = ("vocal", "instrumental")
OTHER = "other"

# Version status: live | staged | wanted | removed | none-exists.
# Source status, and how far along each is. Import merges by keeping the
# furthest state, and the last three are verdicts - never picked again.
_RANK = {"candidate": 0, "staged": 1, "live": 2, "rejected": 3, "replaced": 3, "removed": 3}
BLOCKED = ("rejected", "replaced", "removed")

# Brass bands and orchestras are instrumental unless a collection says otherwise.
_INSTRUMENTAL = re.compile(r"оркестр|orchestra|\bband\b|brass|fanfare|instrumental|инструментал", re.I)
_PUNCT = re.compile(r"[^\w\s]")
# "Марш В боевой поход" and "В боевой поход" are one work, "Марш танкистов" is
# not "Танкистов" - so the prefix is only a hint for a human, never a merge.
_MARCH = re.compile(r"^(торжественный |встречный |парадный )?марш ")


def norm(title):
    """Comparison key for a title: casefold, ё->е, no punctuation."""
    text = unicodedata.normalize("NFC", clean(title or "")).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", text).replace("_", " ")).strip()


def song_id(theme, title):
    return "{}/{}".format(theme, norm(title).replace(" ", "-"))


def split_collection(name):
    """'sov vocal' -> ('sov', 'vocal'). Anything else is theme 'other', version unknown."""
    words = (name or "").split()
    if len(words) >= 2 and words[-1].lower() in VERSIONS:
        return " ".join(words[:-1]).lower(), words[-1].lower()
    return OTHER, None


def guess_version(performer):
    return "instrumental" if _INSTRUMENTAL.search(performer or "") else "vocal"


def source_id(url):
    """(provider, stable id) for a URL: yt:<video>, sovmusic:<fname>, archive.org:<item/file>."""
    provider, key = providers.parse(url)
    if provider == "youtube":
        return provider, "yt:" + key
    if provider:
        return provider, "{}:{}".format(provider, key)
    host = urlparse(url or "").hostname or "unknown"
    return host, host + ":" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:10]


# -- storage -------------------------------------------------------------


def load(path=None):
    path = Path(path or CATALOG)
    if not path.exists():
        return {"songs": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save(cat, path=None):
    """Write atomically: other mlib processes read this file while we work."""
    path = Path(path or CATALOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("{}.{}.tmp".format(path.name, os.getpid()))
    tmp.write_text(json.dumps(cat, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def edit(path=None):
    """Load, change, save - holding a lock so concurrent mlib runs take turns.
    Nothing is saved if the block raises."""
    path = Path(path or CATALOG)
    with file_lock(path.with_name(path.name + ".lock"), timeout=60, stale=120):
        cat = load(path)
        yield cat
        save(cat, path)


# -- lookups -------------------------------------------------------------


def _names(song):
    return {norm(song["title"])} | {norm(a) for a in song.get("aliases") or []}


def find(cat, ref, theme=None):
    """Song ids matching an id, a title or an alias; substring as a last resort."""
    songs = cat["songs"]
    if ref in songs:
        return [ref]
    key = norm(ref)
    pool = [(sid, s) for sid, s in songs.items() if not theme or s["theme"] == theme]
    exact = [sid for sid, s in pool if key in _names(s)]
    if exact:
        return exact
    return [sid for sid, s in pool if any(key in n for n in _names(s))] if key else []


def song_for(cat, theme, title, create=True):
    """The id of the song with this title in this theme, creating it if asked."""
    key = norm(title)
    for sid, song in cat["songs"].items():
        if song["theme"] == theme and key in _names(song):
            return sid
    if not create:
        return None
    sid = song_id(theme, title)
    cat["songs"].setdefault(sid, {"title": clean(title), "theme": theme, "aliases": [],
                                  "notes": "", "versions": {}})
    return sid


def version(song, name, collection=None, album=None):
    v = song["versions"].setdefault(name, {
        "collection": None, "album": None, "current": None, "library_path": None,
        "status": "wanted", "sources": [],
    })
    v["collection"] = v["collection"] or collection
    v["album"] = v["album"] or (None if collection else album)
    return v


def source(v, sid):
    return next((s for s in v["sources"] if s["id"] == sid), None) if sid else None


def locate(cat, url=None, sid=None):
    """(song id, version name, source) holding this URL or source id, else None."""
    sid = sid or (source_id(url)[1] if url else None)
    for song_key, song in cat["songs"].items():
        for vname, v in song["versions"].items():
            hit = source(v, sid)
            if hit:
                return song_key, vname, hit
    return None


def blocked(cat, url):
    """The source if this URL was already judged bad, so callers can refuse it."""
    hit = locate(cat, url)
    return hit[2] if hit and hit[2]["status"] in BLOCKED else None


def next_candidate(v, provider=None):
    return next((s for s in v["sources"] if s["status"] == "candidate"
                 and (not provider or s.get("provider") == provider)), None)


def find_exact(cat, ref):
    """Song ids whose id, title or alias is exactly `ref` - no substring guessing."""
    if ref in cat["songs"]:
        return [ref]
    return [sid for sid, s in cat["songs"].items() if norm(ref) in _names(s)]


def performer(v):
    """Who the version is by: the current source, else the most advanced one."""
    cur = source(v, v.get("current"))
    if cur:
        return cur.get("performer")
    live = [s for s in v["sources"] if s["status"] not in BLOCKED]
    live.sort(key=lambda s: -_RANK[s["status"]])
    return live[0].get("performer") if live else None


def missing(song, vname):
    """Not in the library, not on its way, and not known to be impossible."""
    v = song["versions"].get(vname)
    return v is None or v["status"] == "wanted"


# -- state changes -------------------------------------------------------


def settle(v):
    """Derive the version's current source and status from its sources."""
    cur = source(v, v.get("current"))
    if not cur or cur["status"] != "live":
        live = [s for s in v["sources"] if s["status"] == "live"]
        cur = live[-1] if live else None
    v["current"] = cur["id"] if cur else None
    v["library_path"] = cur.get("library_path") if cur else None
    if cur:
        v["status"] = "live"
    elif any(s["status"] == "staged" for s in v["sources"]):
        v["status"] = "staged"
    elif v["status"] not in ("removed", "none-exists"):
        v["status"] = "wanted"


def _set(s, status, reason=None, merge=False):
    """Move a source to `status`. With merge, never move it backwards."""
    if merge and _RANK[status] < _RANK[s["status"]]:
        return
    if merge and status == s["status"]:
        s["reason"] = s.get("reason") or reason
        return
    s["status"] = status
    if reason is not None or status in ("live", "staged"):
        s["reason"] = reason
    if status != "live":
        s["library_path"] = None


def record(cat, url, title, performer=None, status="candidate", collection=None, album=None,
           theme=None, vname=None, song=None, reason=None, library_path=None, merge=False,
           extra=None):
    """Put a source in the catalog (or update it). Returns (song id, version name, source).

    Placement, most explicit first: `song`/`vname` given, else where this URL
    already sits, else derived from the collection ('sov vocal') and the title.
    `extra` (e.g. year, flags) fills fields the source does not have yet.
    """
    c_theme, c_version = split_collection(collection)
    theme = theme or c_theme
    existing = locate(cat, url) if url else None
    if song:
        sid = song if song in cat["songs"] else song_for(cat, theme, song)
    elif existing:
        sid = existing[0]
    else:
        sid = song_for(cat, theme, title)
    if not vname:
        vname = existing[1] if existing and not song else (c_version or guess_version(performer))

    v = version(cat["songs"][sid], vname, collection=collection, album=album)
    if url:
        url = providers.canonical_url(url)
        provider, src_id = source_id(url)
    else:   # a live file whose origin is unknown
        provider, src_id = "unknown", "file:" + hashlib.sha1(
            (library_path or title).encode("utf-8")).hexdigest()[:10]
    s = source(v, src_id)
    if s is None:
        s = {"id": src_id, "provider": provider, "url": url, "performer": performer,
             "title": clean(title), "status": status, "reason": reason,
             "added": date.today().isoformat(), "library_path": None}
        v["sources"].append(s)
    else:
        s["performer"] = s.get("performer") or performer
        s["title"] = s.get("title") or clean(title)
        _set(s, status, reason, merge=merge)
    for key, val in (extra or {}).items():
        if s.get(key) is None and val is not None:
            s[key] = val
    if status == "live" and s["status"] == "live" and library_path:
        s["library_path"] = library_path
        v["current"] = s["id"]
    if s["status"] == "removed" and v["status"] == "wanted":
        v["status"] = "removed"     # taken out on purpose; not something to look for
    settle(v)
    return sid, vname, s


def _meta_source(cat, meta, create=True):
    """The catalog entry a staged file's sidecar points at."""
    hit = locate(cat, url=meta.get("source"), sid=meta.get("source_id"))
    if hit or not create:
        return hit
    collection = meta.get("album") if meta.get("collection") else None
    return record(cat, meta.get("source"), meta.get("title") or "", meta.get("artist"),
                  status="staged", collection=collection, album=meta.get("album"),
                  vname=meta.get("version"), song=meta.get("song"))


def stage(cat, meta, song=None, vname=None):
    """A file was downloaded into staging. Returns the ids to put in its sidecar."""
    collection = meta.get("album") if meta.get("collection") else None
    sid, vname, s = record(cat, meta.get("source"), meta["title"], meta.get("artist"),
                           status="staged", collection=collection, album=meta.get("album"),
                           song=song, vname=vname)
    return {"song": sid, "version": vname, "source_id": s["id"]}


def published(cat, meta, target):
    """`approve` put a staged file at `target`. Returns the replaced source, if any."""
    sid, vname, s = _meta_source(cat, meta)
    v = cat["songs"][sid]["versions"][vname]
    old = None
    replaces = meta.get("replaces") or {}
    for other in v["sources"]:
        if other is s or other["status"] != "live":
            continue
        # The file it was swapped in for, or one it just overwrote on disk.
        if other["id"] == replaces.get("source_id") or other.get("library_path") == target:
            _set(other, "replaced", replaces.get("reason") or "replaced by " + s["id"])
            old = other
    _set(s, "live")
    s["library_path"] = target
    v["current"] = s["id"]
    settle(v)
    return old


def rejected(cat, meta, reason):
    sid, vname, s = _meta_source(cat, meta)
    _set(s, "rejected", reason)
    settle(cat["songs"][sid]["versions"][vname])
    return sid


def removed(cat, path, reason=None):
    """`rm` deleted a library file. Keeps the history; returns the song id or None."""
    for sid, song in cat["songs"].items():
        for v in song["versions"].values():
            for s in v["sources"]:
                if s["status"] == "live" and s.get("library_path") == path:
                    _set(s, "removed", reason or "removed from the library")
                    v["status"] = "removed"
                    settle(v)
                    return sid
    return None


def note_check(cat, meta):
    """Remember why `check` held a staged file, so a later reject can say so."""
    verdict = (meta.get("check") or {}).get("verdict")
    hit = _meta_source(cat, meta, create=False)
    if hit and verdict and verdict != "ok" and hit[2]["status"] == "staged":
        hit[2]["reason"] = "held: {}: {}".format(verdict, meta["check"]["detail"])


def check_reason(meta):
    """'speech: 0.9 words/s: ...' for a held file, else None."""
    c = meta.get("check") or {}
    if c.get("verdict") in (None, "ok"):
        return None
    return "{}: {}".format(c["verdict"], c.get("detail") or "")


# -- finding sources -----------------------------------------------------


def title_matches(title, names):
    """Is a search result this song? Exact on the normalized title, allowing a
    '(subtitle)' and a 'Composer - ' prefix ("Блантер - Катюша")."""
    bare = re.sub(r"\([^)]*\)", " ", title or "")
    parts = [bare] + re.split(r"\s+[-–—]\s+", bare)
    return any(norm(p) in names for p in parts)


def pick_sources(song, vname, results, cat=None):
    """Filter and rank search results for one version. Returns (kept, dropped)
    where dropped is a list of (result, why).

    Dropped: another song, film excerpts, foreign-language covers, the wrong
    version (a known instrumental for vocal or vice versa), already judged bad.
    Ranked: right version known, performer known, the usual lyrics (a parody
    with other words sorts last), then the oldest recording first."""
    names = _names(song)
    want = vname == "vocal"
    kept, dropped = [], []
    for r in results:
        bad = blocked(cat, r["url"]) if cat else None
        why = ("another song" if not title_matches(r["title"], names)
               else "film" if "film" in r.get("flags", ())
               else "foreign-language" if "foreign-language" in r.get("flags", ())
               else "wrong version" if r.get("vocal") is not None and r["vocal"] != want
               else "already " + bad["status"] if bad
               else None)
        if why:
            dropped.append((r, why))
        else:
            kept.append(r)
    usual = Counter(_lyricist(r) for r in kept if _lyricist(r)).most_common(1)
    usual = usual[0][0] if usual else None
    kept.sort(key=lambda r: (r.get("vocal") is None, not r.get("performer"),
                             bool(usual and _lyricist(r) and _lyricist(r) != usual),
                             r.get("year") is None, r.get("year") or 0))
    return kept, dropped


def _lyricist(result):
    """Surname of the lyricist in sovmusic credits, for spotting other texts."""
    m = re.search(r"Слова:\s*(.*?)(?:\s+\d{4}\s*г|\s+Исполняет|$)", result.get("credits") or "")
    return norm(m.group(1)).split()[-1] if m and norm(m.group(1)) else None


# -- bootstrap -----------------------------------------------------------


def parse_library_path(rel):
    """(collection, album, artist, title) from a library path - see naming.relpath."""
    parts = PurePosixPath(rel).with_suffix("").parts
    if len(parts) == 3:
        title = re.sub(r"^\d+\s*-\s*", "", parts[2])
        return None, parts[1], parts[0], title
    head, _, tail = parts[-1].partition(" - ")
    if len(parts) == 2:
        return parts[0], parts[0], head if tail else None, tail or head
    return None, "Singles", head if tail else None, tail or head


def _stem(rel):
    return str(PurePosixPath(rel).with_suffix(""))


def import_all(cat, listing, history=(), sidecars=(), picks=(), theme="sov"):
    """Merge what is known into the catalog. Re-runnable: sources merge by id.

    listing  - library-relative paths that are live right now
    history  - dicts: artist, title, url, status (live|rejected|removed), info
               (the collection or 'Artist/Album' when live, else the reason), and
               optionally collection (for rows that are not live)
    sidecars - staging sidecar dicts
    picks    - dicts: url, artist, title, collection
    theme    - for history rows that carry no collection (rejected/removed ones)
    """
    stats = {"unknown_origin": 0, "gone": []}
    unclaimed = {_stem(p): p for p in listing}

    for row in history:
        status, info = row["status"], row.get("info") or ""
        if status != "live":
            collection = row.get("collection")
            explicit = split_collection(collection)[1] if collection else None
            vname = explicit or guess_version(row["artist"])
            record(cat, row["url"], row["title"], row["artist"], status=status, reason=info,
                   collection=collection or "{} {}".format(theme, vname), vname=explicit,
                   merge=True)
            continue
        if "/" in info:     # an artist album, not a collection
            collection, album = None, info.split("/", 1)[1]
            stem = "{}/{}".format(info, safe(row["title"]))
        else:
            collection, album = info, info
            stem = "{}/{}".format(info, safe(row["artist"] + " - " + row["title"]))
        path = unclaimed.pop(stem, None) or _claim_by_title(unclaimed, info, row["title"])
        if path:
            record(cat, row["url"], row["title"], row["artist"], status="live",
                   collection=collection, album=album, library_path=path, merge=True)
        else:
            stats["gone"].append("{} - {}".format(row["artist"], row["title"]))
            record(cat, row["url"], row["title"], row["artist"], status="removed",
                   reason="not in the library at import", collection=collection,
                   album=album, merge=True)

    for path in unclaimed.values():
        collection, album, artist, title = parse_library_path(path)
        already = [s for _k, _v, s in _all_sources(cat) if s.get("library_path") == path]
        if already:
            continue
        stats["unknown_origin"] += 1
        record(cat, None, title, artist, status="live", collection=collection, album=album,
               library_path=path, merge=True)

    staged_ids = set()
    for meta in sidecars:
        if not meta.get("source"):
            continue
        collection = meta.get("album") if meta.get("collection") else None
        _sid, _v, s = record(cat, meta["source"], meta.get("title") or "", meta.get("artist"),
                             status="staged", collection=collection, album=meta.get("album"),
                             song=meta.get("song"), vname=meta.get("version"),
                             reason=None, merge=True)
        staged_ids.add(s["id"])
        reason = check_reason(meta)
        if reason and s["status"] == "staged":
            s["reason"] = "held: " + reason

    for pick in picks:
        record(cat, pick["url"], pick["title"], pick["artist"], status="candidate",
               collection=pick["collection"], merge=True)

    # Sync with reality: the listing and staging are complete pictures.
    live = set(listing)
    for _sid, v, s in _all_sources(cat):
        if s["status"] == "live" and s.get("library_path") not in live:
            _set(s, "removed", "no longer in the library")
            v["status"] = "removed"
        elif s["status"] == "staged" and s["id"] not in staged_ids:
            _set(s, "candidate", "left staging without being approved")

    stats["merged"] = merge_aliases(cat)
    for song in cat["songs"].values():
        for v in song["versions"].values():
            settle(v)
    return stats


def _claim_by_title(unclaimed, folder, title):
    """Fallback match: the one file in `folder` with this title, whatever the artist."""
    key = norm(title)
    hits = [stem for stem in unclaimed
            if stem.rsplit("/", 1)[0] == folder
            and norm(parse_library_path(unclaimed[stem])[3]) == key]
    return unclaimed.pop(hits[0]) if len(hits) == 1 else None


def _all_sources(cat):
    for sid, song in cat["songs"].items():
        for v in song["versions"].values():
            for s in v["sources"]:
                yield sid, v, s


def merge_aliases(cat):
    """Fold a song into another whose aliases name it. Returns 'a <- b' lines."""
    merged = []
    songs = cat["songs"]
    for sid in sorted(songs):
        if sid not in songs:
            continue
        song = songs[sid]
        names = {norm(a) for a in song.get("aliases") or []}
        for other_id in sorted(songs):
            other = songs.get(other_id)
            if other_id == sid or other is None or other["theme"] != song["theme"]:
                continue
            if norm(other["title"]) not in names:
                continue
            for vname, ov in other["versions"].items():
                v = version(song, vname, collection=ov["collection"], album=ov["album"])
                for s in ov["sources"]:
                    if not source(v, s["id"]):
                        v["sources"].append(s)
                if ov["status"] in ("removed", "none-exists") and v["status"] == "wanted":
                    v["status"] = ov["status"]
                settle(v)
            for alias in other.get("aliases") or []:
                if norm(alias) not in _names(song):
                    song["aliases"].append(alias)
            del songs[other_id]
            merged.append("{} <- {}".format(sid, other_id))
    return merged


def near_matches(cat, cutoff=0.92):
    """Pairs of songs in one theme that might be the same work. For a human to judge."""
    out = []
    items = sorted(cat["songs"].items())
    for i, (a_id, a) in enumerate(items):
        for b_id, b in items[i + 1:]:
            if a["theme"] != b["theme"]:
                continue
            x, y = norm(a["title"]), norm(b["title"])
            bare_x, bare_y = _MARCH.sub("", x), _MARCH.sub("", y)
            if bare_x == bare_y or difflib.SequenceMatcher(None, x, y).ratio() >= cutoff:
                out.append((a_id, b_id))
    return out
