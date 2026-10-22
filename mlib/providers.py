"""Where audio comes from. Each provider can search and download; the catalog
records which one each source belongs to.

- youtube   yt-dlp (fetch.py), the original path. Anything yt-dlp can open.
- sovmusic  sovmusic.ru: Soviet records with composer, lyricist, performer and
            year. Plain HTTP, cp1251. The site bans the IP (or subnet) of any
            client that opens parallel connections, so every request takes one
            cross-process lock and waits SOVMUSIC_GAP seconds after the previous
            request - including one made by another mlib run.
- archive   archive.org: 78s and LPs, best for the emigre Cossack choirs. Its
            search only understands English or transliterated titles.

A search result is a dict: provider, id, url, title, raw_title, performer,
channel, year (of the recording, when known), duration, vocal (True / False /
None for unknown) and flags ("film", "foreign-language", "community", ...).
"""
import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from . import fetch, tag
from .config import STAGING
from .lock import file_lock, touch

PROVIDERS = ("youtube", "sovmusic", "archive.org")
ALIASES = {"archive": "archive.org", "yt": "youtube"}

HEADERS = {"User-Agent": "Mozilla/5.0 (mlib; personal music library)"}

SOVMUSIC_GAP = 4.0
SOVMUSIC_STATE = Path.home() / ".cache" / "mlib" / "sovmusic.last"
SOVMUSIC_SEARCH = "http://sovmusic.ru/result.php?part=1&type=simple&searchtype=name&searchterm="
SOVMUSIC_FILE = "http://www.sovmusic.ru/m/{}.mp3"     # never /m32/ - a 32 kbps preview


def name(provider):
    """Canonical provider name from a CLI spelling ('archive' -> 'archive.org')."""
    provider = (provider or "youtube").lower()
    provider = ALIASES.get(provider, provider)
    if provider not in PROVIDERS:
        raise ValueError("unknown provider: {} (one of {})".format(provider, ", ".join(PROVIDERS)))
    return provider


def parse(url):
    """(provider, key) for a URL we can identify, else (None, None).

    The key is stable across URL spellings: a YouTube video id, a sovmusic fname,
    an archive.org '<identifier>' or '<identifier>/<file>'."""
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    for prefix in ("www.", "m.", "music."):
        host = host[len(prefix):] if host.startswith(prefix) else host
    if host == "sovmusic.ru":
        m = re.search(r"/m(?:32)?/(\w+)\.mp3$", u.path)
        fname = m.group(1) if m else parse_qs(u.query).get("fname", [None])[0]
        if fname:
            return "sovmusic", fname
    if host == "archive.org":
        parts = unquote(u.path).strip("/").split("/", 2)
        if len(parts) >= 2 and parts[0] in ("details", "download") and parts[1]:
            return "archive.org", "/".join(parts[1:])
    if host in ("youtube.com", "youtu.be"):
        vid = u.path.strip("/") if host == "youtu.be" else parse_qs(u.query).get("v", [None])[0]
        if not vid and u.path.startswith("/shorts/"):
            vid = u.path.split("/")[2]
        if vid:
            return "youtube", vid
    return None, None


def detect(url):
    """Which provider downloads this URL. Anything unrecognised goes to yt-dlp."""
    return parse(url)[0] or "youtube"


def canonical_url(url):
    provider, key = parse(url)
    if provider == "sovmusic":
        return SOVMUSIC_FILE.format(key)
    return url


# -- the three entry points ---------------------------------------------


def search(query, provider="youtube", limit=8):
    provider = name(provider)
    if provider == "sovmusic":
        return sovmusic_search(query)[:limit]
    if provider == "archive.org":
        return archive_search(query, limit=limit)
    out = fetch.search(query, limit=limit)
    for r in out:
        r.update(provider="youtube", performer=r.get("artist"), year=None, vocal=None, flags=[])
    return out


def probe(url):
    """Metadata for one URL, as fetch.probe returns it. sovmusic costs no request."""
    provider, key = parse(url)
    if provider == "sovmusic":
        return {"id": key, "url": SOVMUSIC_FILE.format(key), "raw_title": None,
                "artist": "", "album": "", "title": "", "duration": None}
    if provider == "archive.org":
        track = _archive_track(key)
        return {"id": track["id"], "url": track["url"], "raw_title": track["title"],
                "artist": track["performer"] or "", "album": "", "title": track["title"],
                "duration": track["duration"]}
    return fetch.probe(url)


def download(url, dest_dir=None):
    """Download one URL into staging. Returns (path, info).

    Files from sovmusic and archive.org are named <provider>-<id>.<ext> and have
    every tag stripped: sovmusic's are cp1251 mojibake, and the server-side
    tagger only fills empty fields, so garbage left here would stay forever."""
    provider, key = parse(url)
    if provider not in ("sovmusic", "archive.org"):
        return fetch.download(url, dest_dir)
    dest = Path(dest_dir or STAGING)
    dest.mkdir(parents=True, exist_ok=True)
    if provider == "sovmusic":
        path = dest / "sovmusic-{}.mp3".format(key)
        sovmusic_get(SOVMUSIC_FILE.format(key), path)
        info = {"id": key, "provider": provider}
    else:
        track = _archive_track(key)
        path = dest / "archive-{}{}".format(_safe_id(track["id"]), Path(track["file"]).suffix)
        _stream(track["url"], path)
        info = {"id": track["id"], "provider": provider}
    tag.wipe(path)
    return path, info


# -- sovmusic.ru ---------------------------------------------------------


def sovmusic_get(url, dest=None):
    """One request to sovmusic: never two at once, never closer than SOVMUSIC_GAP
    seconds apart, across processes. Returns the body, or writes it to `dest`."""
    lockfile = SOVMUSIC_STATE.with_suffix(".lock")
    with file_lock(lockfile, timeout=1800, stale=300):
        try:
            last = float(SOVMUSIC_STATE.read_text())
        except (OSError, ValueError):
            last = 0.0
        wait = min(SOVMUSIC_GAP, last + SOVMUSIC_GAP - time.time())
        if wait > 0:
            time.sleep(wait)
        try:
            req = Request(url, headers=dict(HEADERS, Referer="http://sovmusic.ru/"))
            with urlopen(req, timeout=120) as resp:
                if dest is None:
                    return resp.read()
                return _save(resp, dest, keepalive=lockfile)
        finally:
            SOVMUSIC_STATE.write_text(str(time.time()))


def sovmusic_search(query):
    term = quote(query.encode("cp1251", "ignore"))
    page = sovmusic_get(SOVMUSIC_SEARCH + term).decode("cp1251", "replace")
    return parse_sovmusic(page)


# Each hit: the title link, then an <i> block of description and credits that
# ends at the comments link. sam_download.php hits are the amateur section.
_SOV_ENTRY = re.compile(
    r"(?<!sam_)download\.php\?fname=(\w+)><b>(.*?)</a><br><i>(.*?)Комментариев", re.S)
_LANGUAGE = re.compile(r"\s[-–]\s*(\w+(?:ский|цкий)|иврит|идиш|эсперанто)\s*$", re.I)
_FILM = re.compile(r"к/ф|м/ф|кинофильм|из фильма", re.I)


def _text(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_sovmusic(page):
    """Search results from a decoded result.php page."""
    out = []
    for m in _SOV_ENTRY.finditer(page):
        fname, title, body = m.group(1), _text(m.group(2)), _text(m.group(3))
        at = body.find("Музыка:")
        credits, desc = (body[at:], body[:at]) if at >= 0 else ("", body)
        who = re.search(r"Исполняет:\s*(.*?)(?:\s*Исполнение\s+\d{4}\s*г\.?.*)?$", credits)
        performer = who.group(1).strip(" .") if who else None
        recorded = re.search(r"Исполнение\s+(\d{4})", credits)
        written = re.search(r"(\d{4})\s*г\.", credits)

        flags = []
        if _LANGUAGE.search(title):
            flags.append("foreign-language")
        if _FILM.search(performer or "") or _FILM.search(title):
            flags.append("film")
        # No lyricist credit means a band recording. A description that opens
        # with quoted lyrics but credits no lyricist is a song all the same.
        if "Слова:" in credits:
            vocal = True
        elif re.match(r"[\"«„“]", desc):
            vocal = None
        else:
            vocal = False

        out.append({
            "provider": "sovmusic", "id": fname, "url": SOVMUSIC_FILE.format(fname),
            "page": "http://sovmusic.ru/download.php?fname=" + fname,
            "title": title, "raw_title": title, "performer": performer, "channel": None,
            "year": int(recorded.group(1)) if recorded else None,
            "written": int(written.group(1)) if written else None,
            "duration": None, "vocal": vocal, "flags": flags, "credits": credits,
        })
    return out


# -- archive.org ---------------------------------------------------------


def _get_json(url):
    with urlopen(Request(url, headers=HEADERS), timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def archive_search(query, limit=8):
    """Tracks from items matching `query` (English or transliterated - Cyrillic
    titles find next to nothing). One request per item for its file list."""
    params = [("q", "({}) AND mediatype:audio".format(query)), ("rows", limit),
              ("output", "json")] + [("fl[]", f) for f in ("identifier", "title", "creator")]
    docs = _get_json("https://archive.org/advancedsearch.php?" + urlencode(params))
    out = []
    for doc in docs.get("response", {}).get("docs", []):
        out += archive_tracks(doc["identifier"])
        if len(out) >= limit:
            break
    return out[:limit]


def archive_tracks(identifier):
    """One result per audio track of an item: MP3 when there is one, else FLAC."""
    meta = _get_json("https://archive.org/metadata/" + quote(identifier))
    item = meta.get("metadata") or {}
    if str(item.get("access-restricted-item", "")).lower() == "true":
        return []
    collections = item.get("collection") or []
    collections = [collections] if isinstance(collections, str) else collections
    flags = ["community"] if {"opensource_audio", "community"} & set(collections) else []
    year = re.match(r"\d{4}", str(item.get("date") or ""))

    groups = {}     # derivatives point at their original; one group per track
    for f in meta.get("files") or []:
        fname = f.get("name") or ""
        if not fname.lower().endswith((".mp3", ".flac")):
            continue
        key = f.get("original") if f.get("source") == "derivative" else fname
        groups.setdefault(key, []).append(f)

    out = []
    for key, files in groups.items():
        files.sort(key=lambda f: 0 if f["name"].lower().endswith(".mp3") else 1)
        best = files[0]
        title = next((f.get("title") for f in files if f.get("title")), None)
        out.append({
            "provider": "archive.org", "id": "{}/{}".format(identifier, best["name"]),
            "file": best["name"],
            "url": "https://archive.org/download/{}/{}".format(identifier, quote(best["name"])),
            "title": title or item.get("title") or Path(best["name"]).stem,
            "raw_title": "{} [{}]".format(title or Path(best["name"]).stem, identifier),
            "performer": next((f.get("creator") for f in files if f.get("creator")),
                              _first(item.get("creator"))),
            "channel": None, "year": int(year.group(0)) if year else None,
            "duration": _seconds(best.get("length")), "vocal": None, "flags": list(flags),
        })
    return out


def _archive_track(key):
    """The one track a details/download URL means; an album needs the file named."""
    identifier, _, fname = key.partition("/")
    tracks = archive_tracks(identifier)
    if fname:
        hit = [t for t in tracks if t["file"] == fname or t["id"] == key]
        if hit:
            return hit[0]
        raise RuntimeError("no audio file {} in archive.org item {}".format(fname, identifier))
    if len(tracks) == 1:
        return tracks[0]
    names = "\n  ".join(t["url"] for t in tracks[:20])
    raise RuntimeError("archive.org item {} has {} tracks - pass one of:\n  {}".format(
        identifier, len(tracks), names))


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def _seconds(length):
    """archive.org lengths come as '113.87' or '1:53'."""
    try:
        if ":" in str(length):
            parts = [float(p) for p in str(length).split(":")]
            return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
        return float(length) if length else None
    except ValueError:
        return None


def _safe_id(key):
    return re.sub(r"[^\w.-]+", "_", Path(key).with_suffix("").as_posix())[:100]


# -- plumbing ------------------------------------------------------------


def _stream(url, dest):
    with urlopen(Request(url, headers=HEADERS), timeout=120) as resp:
        return _save(resp, dest)


def _save(resp, dest, keepalive=None):
    """Write a response to `dest` via a .part file, so a failed download leaves nothing."""
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    with open(part, "wb") as fh:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            if keepalive:
                touch(keepalive)
    os.replace(part, dest)
    return dest
