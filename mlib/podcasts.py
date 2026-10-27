"""Podcast subscriptions: keep the newest unheard episode of each show in the library.

Navidrome has no podcast feature, so an episode is published as an ordinary
track. Per show the library holds `keep` episodes (default 1). Once Navidrome
reports one as heard (played, and the last play a grace period ago - Amperfy
scrobbles it), `sync` deletes it, remembers its guid as heard, and fetches the
next episode newer than anything heard. Older episodes never come back, and
neither does a heard one.

    shows[<name>] = {name, feed | youtube, keep, min_minutes, title_exclude,
                     live: [{guid, title, published, relpath}],
                     heard: [guid], heard_until, failed: {guid: count}, added}

The state lives in the library itself, at .mlib/podcasts.json, so the laptop
(WebDAV) and the server's hourly timer (direct) read and write one file.
Navidrome and the tagger skip dot-folders, and dufs serves them (it runs without
--hidden). Every change is a read-modify-write in one short step, so the two
never undo each other's heard lists. catalog/podcasts.json, where the state
first lived, is now only a backup copy written after each change, so the
private catalog repo keeps its history.

Layout: <Show>/<Show>/<YYYY-MM-DD - Episode>.<ext>, i.e. artist = album = the
show. That is the ordinary <Artist>/<Album>/<Title> shape, so the server-side
tagger derives the same tags mlib writes (a leading year is not a track number:
TRACKNUM takes at most three digits). A `Podcasts/` prefix would read as artist
"Podcasts"; genre Podcast is what sets the episodes apart instead.
"""
import hashlib
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

from . import fetch, navidrome, store, tag
from .config import REPO_ROOT, STAGING, cfg
from .lock import LockTimeout, file_lock, touch
from .naming import clean, relpath, safe

STATE = ".mlib/podcasts.json"                           # library-relative
BACKUP = REPO_ROOT / "catalog" / "podcasts.json"
# Per machine: the state is shared, but each machine serializes its own runs.
LOCKS = Path.home() / ".cache" / "mlib"
EDIT_LOCK = LOCKS / "podcasts.lock"
SYNC_LOCK = LOCKS / "podcasts.sync.lock"
MAX_FAILURES = 3        # a guid that failed this often is skipped for good
# A subfolder, so `mlib staged` / `approve` never see a half-done episode.
DOWNLOADS = STAGING / "podcasts"
GENRE = "Podcast"
HEADERS = {"User-Agent": "mlib-podcasts/0.1 (+self-hosted Navidrome)"}
ITUNES = "https://itunes.apple.com/search"
YOUTUBE_LIMIT = 15      # newest uploads looked at per channel/playlist

_ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
_EXT = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/x-m4a": "m4a", "audio/mp4": "m4a",
        "audio/m4a": "m4a", "audio/aac": "aac", "audio/ogg": "ogg", "audio/opus": "opus"}


class PodcastError(RuntimeError):
    pass


# -- storage -------------------------------------------------------------


def _dump(state):
    return json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load(log=print):
    """The shared state, or None when there is none yet. The first run after the
    move uploads catalog/podcasts.json into the library."""
    text = store.read_text(STATE)
    if text is not None:
        return json.loads(text)
    if not BACKUP.exists():
        return None
    state = json.loads(BACKUP.read_text(encoding="utf-8"))
    store.write_text(STATE, _dump(state))
    log("migrated catalog/{} into the library as {}".format(BACKUP.name, STATE))
    return state


def save(state):
    store.write_text(STATE, _dump(state))
    if BACKUP.parent.is_dir():          # the catalog clone; absent on the server
        tmp = BACKUP.with_name("{}.{}.tmp".format(BACKUP.name, os.getpid()))
        tmp.write_text(_dump(state), encoding="utf-8")
        os.replace(tmp, BACKUP)


@contextmanager
def edit():
    """Re-read, change, save in one short step under the local lock. Another
    machine may have written since we last looked; this keeps its changes.
    Nothing is saved if the block raises."""
    with file_lock(EDIT_LOCK, timeout=60, stale=120):
        state = load() or {"shows": {}}
        yield state
        save(state)


def find(state, ref, exact=False):
    """Show names matching `ref`: exact (casefold) first, else every substring hit
    - unless `exact`, which `pod add` uses so a new show is never taken for an old."""
    key = (ref or "").casefold()
    names = list(state["shows"])
    hits = [n for n in names if n.casefold() == key]
    return hits if hits or exact else [n for n in names if key in n.casefold()]


def new_show(name, feed=None, youtube=None, keep=1, min_minutes=None, title_exclude=None):
    return {"name": name, "feed": feed, "youtube": youtube, "keep": keep,
            "min_minutes": min_minutes, "title_exclude": title_exclude,
            "live": [], "heard": [], "heard_until": None, "failed": {},
            "added": date.today().isoformat()}


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


def _pubdate(text):
    """RFC 822 as RSS wants it; some feeds send ISO 8601 instead."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return _iso(parsedate_to_datetime(text))
    except (TypeError, ValueError):
        pass
    try:
        return _iso(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


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
        out.append({
            "guid": (item.findtext("guid") or url).strip(),
            "title": clean(item.findtext("title") or "") or "Untitled",
            "published": _pubdate(item.findtext("pubDate")),
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
    and the list is cut at the first episode published before the newest heard
    one. Without dates (flat YouTube listings) it is cut by position instead, at
    the first heard or live entry. Once the newest has been heard, nothing comes
    until a newer one is published."""
    heard = set(show.get("heard") or [])
    live = {e["guid"] for e in show.get("live") or []}
    failed = show.get("failed") or {}
    until = show.get("heard_until")
    want = max(0, int(show.get("keep") or 1) - len(live))
    picked = []
    for ep in entries:
        if len(picked) >= want:
            break
        if until and ep.get("published"):
            if ep["published"] < until:     # `<`: another episode in the same second counts
                break
        elif ep["guid"] in heard or (not ep.get("published") and ep["guid"] in live):
            break
        if ep["guid"] in heard or ep["guid"] in live or not passes(show, ep):
            continue
        if failed.get(ep["guid"], 0) >= MAX_FAILURES:
            continue
        picked.append(ep)
    return picked


def episode_title(ep):
    """What the library shows: 'YYYY-MM-DD - <title>', so episodes sort by date."""
    day = (ep.get("published") or date.today().isoformat())[:10]
    return "{} - {}".format(day, ep["title"])


def show_dir(show):
    return "{0}/{0}/".format(safe(show["name"]))


def target_path(show, ep, ext):
    """<Show>/<Show>/<YYYY-MM-DD - Title>.<ext>. The title is cut so the whole name
    fits naming.safe's limit; a name already taken gets a short guid hash."""
    stem = "{} - {}".format(episode_title(ep)[:10], safe(ep["title"])[:100].rstrip(" .-"))
    rel = relpath(show["name"], show["name"], stem, ext=ext)
    if store.exists(rel):
        short = hashlib.sha1(ep["guid"].encode("utf-8")).hexdigest()[:6]
        rel = relpath(show["name"], show["name"], "{} [{}]".format(stem, short), ext=ext)
    return rel


def owned(show, rel):
    """Podcast code deletes only inside the show's own folder."""
    rel = (rel or "").replace("\\", "/")
    return rel.startswith(show_dir(show)) and ".." not in rel.split("/")


# -- fetching and publishing ---------------------------------------------


def download_episode(ep, dest=None, keepalive=None):
    """Download one episode into staging/podcasts. RSS audio is kept as the
    publisher made it (no transcode); YouTube goes through yt-dlp as usual."""
    dest = Path(dest or DOWNLOADS)
    dest.mkdir(parents=True, exist_ok=True)
    if ep["guid"].startswith("yt:"):
        hook = (lambda _progress: touch(keepalive)) if keepalive else None
        path, info = fetch.download(ep["url"], dest, progress=hook)
        if not ep.get("published") and info.get("upload_date"):
            ep["published"] = _iso(datetime.strptime(info["upload_date"], "%Y%m%d"))
        return path

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
    return remux_aac(target) if target.suffix == ".aac" else target


def remux_aac(path):
    """Raw ADTS .aac has nowhere to keep tags; copy the stream into .m4a, no re-encode."""
    path = Path(path)
    out = path.with_suffix(".m4a")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c", "copy", str(out)],
                   check=True)
    path.unlink()
    return out


def publish(show, ep, path):
    """Tag one downloaded episode and put it in the library. Returns its relpath."""
    name = show["name"]
    tag.wipe(path)      # publishers' tags vary; mlib's are the only ones that matter
    tag.apply(path, artist=name, album=name, title=episode_title(ep), albumartist=name,
              genre=GENRE)
    target = target_path(show, ep, Path(path).suffix)
    store.put(path, target)
    return target


# -- the cycle -----------------------------------------------------------


def _update(name, change):
    """Apply `change` to one show in the shared state. False, and nothing saved,
    when the show is gone - unsubscribed meanwhile, here or on another machine."""
    with edit() as state:
        show = state["shows"].get(name)
        if show is None:
            return False
        change(show)
        return True


def _heard(show, ep):
    show["live"] = [e for e in show["live"] if e["guid"] != ep["guid"]]
    if ep["guid"] not in show["heard"]:
        show["heard"].append(ep["guid"])
    if ep.get("published") and ep["published"] > (show.get("heard_until") or ""):
        show["heard_until"] = ep["published"]


def _live(show, ep):
    show["live"] = [e for e in show["live"] if e["guid"] != ep["guid"]] + [ep]
    show.setdefault("failed", {}).pop(ep["guid"], None)


def _failed(show, guid):
    failed = show.setdefault("failed", {})
    failed[guid] = failed.get(guid, 0) + 1


def _played_at(text):
    """Navidrome's `played`: RFC 3339, maybe with more fraction than Python parses."""
    if not text:
        return None
    text = re.sub(r"\.\d+", "", text.strip()).replace("Z", "+00:00")
    try:
        played = datetime.fromisoformat(text)
    except ValueError:
        return None
    return played if played.tzinfo else played.replace(tzinfo=timezone.utc)


def playing_now(log=print):
    """Song ids being played right now, or None when Navidrome cannot say - and
    then nothing is deleted, never on uncertainty."""
    try:
        return navidrome.now_playing()
    except Exception as exc:
        log("warning: cannot read play counts ({}) - heard episodes stay until next sync"
            .format(exc))
        return None


def finished(song, playing, now=None):
    """Heard: played, not playing now, and the last play at least the grace period
    ago - Amperfy may scrobble at the start or halfway through."""
    if not song or int(song.get("playCount") or 0) <= 0 or song.get("id") in playing:
        return False
    played = _played_at(song.get("played"))
    if played is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - played >= timedelta(hours=cfg.pod_grace_hours)


def retire_heard(show, playing, log=print):
    """Step 1: delete live episodes Navidrome says were heard. Returns how many."""
    done = 0
    for ep in list(show.get("live") or []):
        song = navidrome.find_song(ep["title"], show["name"], ep.get("relpath"))
        if song is None:
            if store.exists(ep["relpath"]):
                log("  {}: not in Navidrome yet - kept".format(ep["title"]))
                continue
            log("  gone from the library, counted as heard: " + ep["relpath"])
        elif not finished(song, playing):
            continue
        elif not owned(show, ep["relpath"]):
            log("  refusing to delete outside {}: {}".format(show_dir(show), ep["relpath"]))
            continue
        else:
            store.delete(ep["relpath"])     # False when already gone - heard either way
            log("  heard, deleted: " + ep["relpath"])
        _update(show["name"], lambda s, ep=ep: _heard(s, ep))
        _heard(show, ep)
        done += 1
    return done


def fill(show, log=print, keepalive=None):
    """Step 2: download and publish the newest unheard episodes up to `keep`.
    A failed download is counted and raised; the next sync tries again, and a
    guid that failed MAX_FAILURES times is skipped from then on."""
    done = 0
    for ep in choose(show, fetch_entries(show)):
        log("  downloading: {}".format(episode_title(ep)))
        try:
            local = download_episode(ep, keepalive=keepalive)
            target = publish(show, ep, local)
        except Exception:
            _update(show["name"], lambda s: _failed(s, ep["guid"]))
            raise
        live = {"guid": ep["guid"], "title": episode_title(ep), "published": ep.get("published"),
                "relpath": target}
        if not _update(show["name"], lambda s: _live(s, live)):
            if owned(show, target):
                store.delete(target)
            log("  unsubscribed meanwhile - removed " + target)
            return done + 1
        _live(show, live)
        log("  published: " + target)
        done += 1
    return done


def sync(names=None, log=print):
    """Run the cycle for the given shows (default: all). Returns (changed, failed):
    how many files were added or removed, so the caller knows whether to rescan,
    and the shows that raised. Raises PodcastError when there is nothing to sync -
    an empty state on the server means it is looking at the wrong library."""
    held = file_lock(SYNC_LOCK, timeout=1, stale=3600)
    try:
        # Episodes are big: hold the run lock for hours if need be, and refresh it
        # while downloading so it is never mistaken for a crashed run.
        held.__enter__()
    except LockTimeout:
        log("another podcast sync is running - skipped")
        return 0, []
    try:
        state = load(log)
        if not state or not state.get("shows"):
            raise PodcastError("no podcast subscriptions in {} ({}) - `mlib pod add` first"
                               .format(STATE, cfg.describe()))
        shows = state["shows"]
        names = names or sorted(shows)
        playing = playing_now(log) if any(shows[n].get("live") for n in names) else None
        changed, failed = 0, []
        for name in names:
            show = shows[name]
            log("{}:".format(name))
            try:
                if playing is not None:
                    changed += retire_heard(show, playing, log)
                changed += fill(show, log, keepalive=SYNC_LOCK)
            except Exception as exc:
                log("  FAILED: {}".format(exc))
                failed.append(name)
                continue
            if not show.get("live"):
                log("  nothing new yet")
        return changed, failed
    finally:
        held.__exit__(None, None, None)
