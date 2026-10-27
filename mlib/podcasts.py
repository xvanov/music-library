"""Podcast subscriptions: keep the newest unheard episode of each show in the library.

Navidrome has no podcast feature, so an episode is published as an ordinary
track. Per show the library holds `keep` episodes (default 1). Once Navidrome
reports one as played (playCount > 0 - Amperfy scrobbles it), `sync` deletes
it, remembers its guid as heard, and fetches the next episode newer than
anything heard. Older episodes never come back, and neither does a heard one.

    shows[<name>] = {name, feed | youtube, keep, min_minutes, title_exclude,
                     live: [{guid, title, published, relpath}],
                     heard: [guid], heard_until, added}

Stored as catalog/podcasts.json, next to the song catalog and as private as it.

Layout: <Show>/<Show>/<YYYY-MM-DD - Episode>.<ext>, i.e. artist = album = the
show. That is the ordinary <Artist>/<Album>/<Title> shape, so the server-side
tagger derives the same tags mlib writes (a leading year is not a track number:
TRACKNUM takes at most three digits). A `Podcasts/` prefix would read as artist
"Podcasts"; genre Podcast is what sets the episodes apart instead.
"""
import json
import os
import re
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

from . import fetch, navidrome, store, tag
from .config import REPO_ROOT, STAGING
from .lock import LockTimeout, file_lock, touch
from .naming import clean, relpath

PODCASTS = REPO_ROOT / "catalog" / "podcasts.json"
# A subfolder, so `mlib staged` / `approve` never see a half-done episode.
DOWNLOADS = STAGING / "podcasts"
GENRE = "Podcast"
HEADERS = {"User-Agent": "mlib-podcasts/0.1 (+self-hosted Navidrome)"}
ITUNES = "https://itunes.apple.com/search"
YOUTUBE_LIMIT = 15      # newest uploads looked at per channel/playlist

_ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
_EXT = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/x-m4a": "m4a", "audio/mp4": "m4a",
        "audio/m4a": "m4a", "audio/aac": "aac", "audio/ogg": "ogg", "audio/opus": "opus"}


# -- storage -------------------------------------------------------------


def load(path=None):
    path = Path(path or PODCASTS)
    if not path.exists():
        return {"shows": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save(state, path=None):
    """Write atomically: `pod ls` reads while a sync runs."""
    path = Path(path or PODCASTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("{}.{}.tmp".format(path.name, os.getpid()))
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def edit(path=None):
    """Load, change, save under a lock. Nothing is saved if the block raises."""
    path = Path(path or PODCASTS)
    with file_lock(path.with_name(path.name + ".lock"), timeout=60, stale=120):
        state = load(path)
        yield state
        save(state, path)


def find(state, ref):
    """Show names matching `ref`: exact (casefold) first, else every substring hit."""
    key = (ref or "").casefold()
    names = list(state["shows"])
    exact = [n for n in names if n.casefold() == key]
    return exact or [n for n in names if key in n.casefold()]


def new_show(name, feed=None, youtube=None, keep=1, min_minutes=None, title_exclude=None):
    return {"name": name, "feed": feed, "youtube": youtube, "keep": keep,
            "min_minutes": min_minutes, "title_exclude": title_exclude,
            "live": [], "heard": [], "heard_until": None, "added": date.today().isoformat()}


# -- finding a feed ------------------------------------------------------


def itunes_search(term, limit=5):
    """Podcasts matching `term` in Apple's directory - free, no key."""
    import requests

    r = requests.get(ITUNES, params={"media": "podcast", "entity": "podcast", "term": term,
                                     "limit": limit}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return [{"name": x.get("collectionName"), "artist": x.get("artistName"),
             "feed": x.get("feedUrl"), "episodes": x.get("trackCount"),
             "latest": (x.get("releaseDate") or "")[:10]}
            for x in r.json().get("results", []) if x.get("feedUrl")]


def best_match(term, results):
    """Index of the result to take: an exact title match, else Apple's first.
    Fan and clip feeds copy the title exactly, so among exact matches the
    freshest feed wins - the real show publishes every week, a re-upload feed
    lags or stops."""
    key = clean(term).casefold()
    exact = [i for i, r in enumerate(results) if clean(r["name"]).casefold() == key]
    if not exact:
        return 0
    return max(exact, key=lambda i: (results[i]["latest"] or "", results[i]["episodes"] or 0))


# -- reading episodes ----------------------------------------------------


def _iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds(text):
    """itunes:duration is '5400', '90:00' or '1:30:00'."""
    try:
        parts = [float(p) for p in (text or "").strip().split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3:
        return None
    total = 0
    for p in parts:
        total = total * 60 + p
    return int(total)


def _ext(url, mime):
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix in ("mp3", "m4a", "aac", "ogg", "opus"):
        return suffix
    return _EXT.get((mime or "").split(";")[0].strip().lower(), "mp3")


def parse_feed(xml):
    """Episodes of an RSS feed, newest first: {guid, title, published, url, ext, duration}.
    Items without an audio enclosure (articles, video-only) are dropped."""
    root = ET.fromstring(xml)
    out = []
    for item in root.iter("item"):
        enc = item.find("enclosure")
        url = enc.get("url") if enc is not None else None
        if not url:
            continue
        mime = enc.get("type") or ""
        if mime and not mime.startswith("audio"):
            continue
        published = None
        try:
            published = _iso(parsedate_to_datetime(item.findtext("pubDate") or ""))
        except (TypeError, ValueError):
            pass
        out.append({
            "guid": (item.findtext("guid") or url).strip(),
            "title": clean(item.findtext("title") or "") or "Untitled",
            "published": published,
            "url": url.strip(),
            "ext": _ext(url, mime),
            "duration": _seconds(item.findtext(_ITUNES_NS + "duration")),
        })
    # Feeds are nearly always newest first already; sort to be sure. Undated
    # items keep their place at the end.
    out.sort(key=lambda e: e["published"] or "", reverse=True)
    return out


def feed_title(xml):
    return clean(ET.fromstring(xml).findtext("channel/title") or "")


def get_feed(url):
    import requests

    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.content


def youtube_entries(url, limit=YOUTUBE_LIMIT):
    """The newest uploads of a channel or playlist, in the order YouTube lists them
    (newest first for a channel's videos tab). Flat listings often lack dates."""
    with fetch._ydl({"skip_download": True, "extract_flat": True,
                     "playlistend": limit}) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        published = None
        if e.get("timestamp"):
            published = _iso(datetime.fromtimestamp(e["timestamp"], timezone.utc))
        elif e.get("upload_date"):
            published = _iso(datetime.strptime(e["upload_date"], "%Y%m%d"))
        out.append({"guid": "yt:" + e["id"], "title": clean(e.get("title") or "") or "Untitled",
                    "published": published,
                    "url": "https://www.youtube.com/watch?v=" + e["id"],
                    "ext": None, "duration": e.get("duration")})
    return out


def fetch_entries(show):
    if show.get("youtube"):
        return youtube_entries(show["youtube"])
    return parse_feed(get_feed(show["feed"]))


# -- choosing ------------------------------------------------------------


def passes(show, ep):
    """Show filters: skip trailers and shorts. An unknown duration passes."""
    if show.get("min_minutes") and ep.get("duration") is not None:
        if ep["duration"] < show["min_minutes"] * 60:
            return False
    if show.get("title_exclude") and re.search(show["title_exclude"], ep["title"], re.I):
        return False
    return True


def choose(show, entries):
    """Episodes to fetch now, newest first.

    Only episodes newer than every heard one count: `entries` is newest first,
    so the list is cut at the first heard guid (or anything published no later
    than the newest heard episode). Once the newest has been heard, nothing
    comes until a newer one is published."""
    heard = set(show.get("heard") or [])
    live = {e["guid"] for e in show.get("live") or []}
    until = show.get("heard_until")
    want = max(0, int(show.get("keep") or 1) - len(live))
    picked = []
    for ep in entries:
        if ep["guid"] in heard or (until and ep.get("published") and ep["published"] <= until):
            break
        if len(picked) >= want:
            break
        if ep["guid"] in live or not passes(show, ep):
            continue
        picked.append(ep)
    return picked


def episode_title(ep):
    """What the library shows: 'YYYY-MM-DD - <title>', so episodes sort by date."""
    day = (ep.get("published") or date.today().isoformat())[:10]
    return "{} - {}".format(day, ep["title"])


# -- fetching and publishing ---------------------------------------------


def download_episode(ep, dest=None, keepalive=None):
    """Download one episode into staging/podcasts. RSS audio is kept as the
    publisher made it (no transcode); YouTube goes through yt-dlp as usual."""
    dest = Path(dest or DOWNLOADS)
    dest.mkdir(parents=True, exist_ok=True)
    if ep["guid"].startswith("yt:"):
        path, info = fetch.download(ep["url"], dest)
        if not ep.get("published") and info.get("upload_date"):
            ep["published"] = _iso(datetime.strptime(info["upload_date"], "%Y%m%d"))
        return path

    import hashlib
    import requests

    name = "podcast-{}.{}".format(hashlib.sha1(ep["guid"].encode("utf-8")).hexdigest()[:12],
                                  ep["ext"])
    target = dest / name
    part = target.with_name(name + ".part")
    with requests.get(ep["url"], headers=HEADERS, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(part, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
                if keepalive:
                    touch(keepalive)
    os.replace(part, target)
    return target


def publish(show, ep, path):
    """Tag one downloaded episode and put it in the library. Returns its relpath."""
    name = show["name"]
    title = episode_title(ep)
    tag.wipe(path)      # publishers' tags vary; mlib's are the only ones that matter
    tag.apply(path, artist=name, album=name, title=title, albumartist=name, genre=GENRE)
    target = relpath(name, name, title, ext=Path(path).suffix)
    store.put(path, target)
    return target


# -- the cycle -----------------------------------------------------------


def _update(name, change, path=None):
    with edit(path) as state:
        return change(state["shows"][name])


def _heard(show, ep):
    show["live"] = [e for e in show["live"] if e["guid"] != ep["guid"]]
    if ep["guid"] not in show["heard"]:
        show["heard"].append(ep["guid"])
    if ep.get("published") and ep["published"] > (show.get("heard_until") or ""):
        show["heard_until"] = ep["published"]


def _live(show, ep):
    show["live"] = [e for e in show["live"] if e["guid"] != ep["guid"]] + [ep]


def navidrome_ok(log=print):
    """Play counts need Navidrome. Without it nothing is deleted - never on uncertainty."""
    try:
        navidrome.ping()
        return True
    except Exception as exc:
        log("warning: cannot read play counts ({}) - heard episodes stay until next sync"
            .format(exc))
        return False


def retire_heard(show, log=print, path=None):
    """Step 1: delete live episodes Navidrome says were played. Returns how many."""
    done = 0
    for ep in list(show.get("live") or []):
        try:
            plays = navidrome.play_count(ep["title"], show["name"], ep.get("relpath"))
        except Exception as exc:
            log("  {}: play count unknown ({}) - kept".format(ep["title"], exc))
            continue
        if plays is None:
            log("  {}: not in Navidrome yet - kept".format(ep["title"]))
            continue
        if plays <= 0:
            continue
        store.delete(ep["relpath"])     # False when already gone - heard either way
        _update(show["name"], lambda s, ep=ep: _heard(s, ep), path)
        _heard(show, ep)
        log("  heard, deleted: " + ep["relpath"])
        done += 1
    return done


def fill(show, log=print, path=None, keepalive=None):
    """Step 2: download and publish the newest unheard episodes up to `keep`."""
    try:
        entries = fetch_entries(show)
    except Exception as exc:
        log("  feed unreadable ({}) - skipped".format(exc))
        return 0
    done = 0
    for ep in choose(show, entries):
        log("  downloading: {}".format(episode_title(ep)))
        try:
            local = download_episode(ep, keepalive=keepalive)
            target = publish(show, ep, local)
        except Exception as exc:
            # Try again next sync rather than reaching for an older episode.
            log("  failed ({}) - will retry next sync".format(exc))
            break
        live = {"guid": ep["guid"], "title": episode_title(ep), "published": ep.get("published"),
                "relpath": target}
        _update(show["name"], lambda s: _live(s, live), path)
        _live(show, live)
        log("  published: " + target)
        done += 1
    return done


def sync(names=None, log=print, path=None):
    """Run the cycle for the given shows (default: all). Returns how many files
    were added or removed, so the caller knows whether to rescan."""
    path = Path(path or PODCASTS)
    lock = path.with_name(path.name + ".sync.lock")
    try:
        # Episodes are big: hold the run lock for hours if need be, and refresh it
        # while downloading so it is never mistaken for a crashed run.
        with file_lock(lock, timeout=1, stale=3600):
            shows = load(path)["shows"]
            names = names or sorted(shows)
            nd = navidrome_ok(log) if any(shows[n].get("live") for n in names) else False
            changed = 0
            for name in names:
                show = shows[name]
                log("{}:".format(name))
                if nd:
                    changed += retire_heard(show, log, path)
                changed += fill(show, log, path, keepalive=lock)
                if not show.get("live"):
                    log("  nothing new yet")
            return changed
    except LockTimeout:
        log("another podcast sync is running - skipped")
        return 0
