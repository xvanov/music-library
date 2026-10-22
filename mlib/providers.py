"""Where audio comes from. A provider is one site mlib can search and download
from; the catalog records which provider each source belongs to.

YouTube (via yt-dlp) is the general default and also catches any URL no other
provider claims. sovmusic.ru and archive.org are optional extras - and the
worked examples for adding a site of your own.

Adding a provider (say, Bandcamp) is one class and one line:

    class Bandcamp(Provider):
        name = "bandcamp"

        def parse(self, url):          # a stable key if this URL is ours, else None
            u = urlparse(url)
            return u.path.strip("/") if (u.hostname or "").endswith("bandcamp.com") else None

        def search(self, query, limit):   # -> list of result dicts, see below
            ...

        def download(self, key, url, dest):   # -> (path, info); write into dest
            ...

    REGISTRY.insert(0, Bandcamp())   # before YouTube, which accepts anything

Optional overrides: `canonical(key, url)` (one URL per recording, so the catalog
never stores the same source twice) and `probe(key, url)` (metadata before
downloading). Rules for a provider:

- Name staged files <provider>-<id>.<ext> and call tag.wipe() on them: tags from
  other sites are often wrong or mis-encoded, and the server-side tagger only
  fills empty fields, so garbage left in place stays forever.
- Be polite: serialize requests and pause between them. Never download in
  parallel. See SovMusic, which throttles across separate mlib runs.

A search result is a dict: provider, id, url, title, raw_title, performer,
channel, year (of the recording, when known), duration, vocal (True / False /
None for unknown) and flags ("film", "foreign-language", "community", ...).
`mlib source find` uses vocal and flags to drop the wrong version and junk.

The examples:

- sovmusic  sovmusic.ru: Soviet records with composer, lyricist, performer and
            year. Plain HTTP, cp1251. The site bans the IP (or subnet) of any
            client that opens parallel connections, so every request takes one
            cross-process lock and waits SOVMUSIC_GAP seconds after the previous
            request - including one made by another mlib run.
- archive   archive.org: 78s and LPs. Its search only understands English or
            transliterated titles.
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

HEADERS = {"User-Agent": "Mozilla/5.0 (mlib; personal music library)"}

SOVMUSIC_GAP = 4.0
SOVMUSIC_STATE = Path.home() / ".cache" / "mlib" / "sovmusic.last"
SOVMUSIC_SEARCH = "http://sovmusic.ru/result.php?part=1&type=simple&searchtype=name&searchterm="
SOVMUSIC_FILE = "http://www.sovmusic.ru/m/{}.mp3"     # never /m32/ - a 32 kbps preview


class Provider:
    """The interface. See the module docstring for how to add one."""

    name = None

    def parse(self, url):
        """A stable key for a URL this provider owns (the same for every spelling
        of one recording), else None."""
        return None

    def canonical(self, key, url):
        return url

    def search(self, query, limit):
        raise NotImplementedError("{} cannot search".format(self.name))

    def probe(self, key, url):
        return {"id": key, "url": self.canonical(key, url), "raw_title": None,
                "artist": "", "album": "", "title": "", "duration": None}

    def download(self, key, url, dest):
        raise NotImplementedError


def _host(u):
    host = (u.hostname or "").lower()
    for prefix in ("www.", "m.", "music."):
        host = host[len(prefix):] if host.startswith(prefix) else host
    return host


class YouTube(Provider):
    """yt-dlp: YouTube, and whatever else yt-dlp can open (the fallback)."""

    name = "youtube"

    def parse(self, url):
        u = urlparse(url or "")
        host = _host(u)
        if host not in ("youtube.com", "youtu.be"):
            return None
        vid = u.path.strip("/") if host == "youtu.be" else parse_qs(u.query).get("v", [None])[0]
        if not vid and u.path.startswith("/shorts/"):
            vid = u.path.split("/")[2]
        return vid or None

    def search(self, query, limit):
        out = fetch.search(query, limit=limit)
        for r in out:
            r.update(provider=self.name, performer=r.get("artist"), year=None, vocal=None,
                     flags=[])
        return out

    def probe(self, key, url):
        return fetch.probe(url)

    def download(self, key, url, dest):
        return fetch.download(url, dest)      # keeps yt-dlp's <id>.<ext> naming


class SovMusic(Provider):
    name = "sovmusic"

    def parse(self, url):
        u = urlparse(url or "")
        if _host(u) != "sovmusic.ru":
            return None
        m = re.search(r"/m(?:32)?/(\w+)\.mp3$", u.path)
        return m.group(1) if m else parse_qs(u.query).get("fname", [None])[0]

    def canonical(self, key, url):
        return SOVMUSIC_FILE.format(key)

    def search(self, query, limit):
        return sovmusic_search(query)[:limit]

    def download(self, key, url, dest):
        path = dest / "sovmusic-{}.mp3".format(key)
        sovmusic_get(SOVMUSIC_FILE.format(key), path)
        return path, {"id": key, "provider": self.name}


class Archive(Provider):
    name = "archive.org"

    def parse(self, url):
        u = urlparse(url or "")
        if _host(u) != "archive.org":
            return None
        parts = unquote(u.path).strip("/").split("/", 2)
        if len(parts) >= 2 and parts[0] in ("details", "download") and parts[1]:
            return "/".join(parts[1:])
        return None

    def search(self, query, limit):
        return archive_search(query, limit=limit)

    def probe(self, key, url):
        track = _archive_track(key)
        return {"id": track["id"], "url": track["url"], "raw_title": track["title"],
                "artist": track["performer"] or "", "album": "", "title": track["title"],
                "duration": track["duration"]}

    def download(self, key, url, dest):
        track = _archive_track(key)
        path = dest / "archive-{}{}".format(_safe_id(track["id"]), Path(track["file"]).suffix)
        _stream(track["url"], path)
        return path, {"id": track["id"], "provider": self.name}


# Asked in order; YouTube last because yt-dlp takes any URL the others decline.
REGISTRY = [SovMusic(), Archive(), YouTube()]
ALIASES = {"archive": "archive.org", "yt": "youtube"}
FALLBACK = "youtube"


def get(provider):
    """A provider by name or CLI spelling ('archive' -> archive.org)."""
    wanted = ALIASES.get((provider or FALLBACK).lower(), (provider or FALLBACK).lower())
    for p in REGISTRY:
        if p.name == wanted:
            return p
    raise ValueError("unknown provider: {} (one of {})".format(
        provider, ", ".join(p.name for p in REGISTRY)))


def name(provider):
    return get(provider).name


def parse(url):
    """(provider name, key) for a URL some provider claims, else (None, None)."""
    for p in REGISTRY:
        key = p.parse(url)
        if key:
            return p.name, key
    return None, None


def detect(url):
    """Which provider downloads this URL. Anything unclaimed goes to yt-dlp."""
    return parse(url)[0] or FALLBACK


def canonical_url(url):
    provider, key = parse(url)
    return get(provider).canonical(key, url) if provider else url


# -- the three entry points ---------------------------------------------


def search(query, provider=FALLBACK, limit=8):
    return get(provider).search(query, limit)


def probe(url):
    """Metadata for one URL, as fetch.probe returns it."""
    provider, key = parse(url)
    return get(provider or FALLBACK).probe(key, url)


def download(url, dest_dir=None):
    """Download one URL into staging. Returns (path, info). Files from anywhere
    but yt-dlp have every tag wiped - see the module docstring."""
    provider, key = parse(url)
    p = get(provider or FALLBACK)
    if p.name == FALLBACK:
        return p.download(key, url, dest_dir)
    dest = Path(dest_dir or STAGING)
    dest.mkdir(parents=True, exist_ok=True)
    path, info = p.download(key, url, dest)
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
