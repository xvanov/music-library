#!/usr/bin/env python3
"""Auto-tag audio files in /music from their folder path + filename.

Convention (relative to /music):
  <Artist>/<Album>/<Title>.mp3      -> artist, album, title
  <Album>/<Title>.mp3               -> album = folder, artist from "Artist - Title" or Unknown Artist
  <Title>.mp3                       -> album = Singles,  artist from "Artist - Title" or Unknown Artist

Filenames may be prefixed with a track number: "03 - Title", "03. Title", "03 Title".
Only fills in tags that are missing; never overwrites tags you set yourself.
"""
import os
import re
import time
import unicodedata

from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus

ROOT = "/music"
INTERVAL = int(os.environ.get("TAGGER_INTERVAL", "30"))
EXTS = {".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".opus", ".wav", ".aac"}

TRACKNUM = re.compile(r"^\s*(\d{1,3})\s*[-._)]?\s+(.*)$")
SPLIT = re.compile(r"\s+-\s+")


def clean(s):
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"[_]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # strip common youtube-rip noise
    s = re.sub(r"\s*[\(\[](official\s*)?(music\s*)?(video|audio|lyrics?|hd|hq|4k)[\)\]]\s*$", "", s, flags=re.I)
    return s.strip()


def derive(relpath):
    parts = relpath.split(os.sep)
    stem = os.path.splitext(parts[-1])[0]
    dirs = parts[:-1]

    track = None
    m = TRACKNUM.match(stem)
    if m:
        track, stem = int(m.group(1)), m.group(2)

    artist = album = None
    if len(dirs) >= 2:
        artist, album = clean(dirs[0]), clean(dirs[1])
        title = clean(stem)
    else:
        album = clean(dirs[0]) if dirs else "Singles"
        bits = SPLIT.split(stem, maxsplit=1)
        if len(bits) == 2 and bits[0].strip() and bits[1].strip():
            artist, title = clean(bits[0]), clean(bits[1])
        else:
            artist, title = "Unknown Artist", clean(stem)

    return {
        "artist": artist or "Unknown Artist",
        "album": album or "Singles",
        "title": title or os.path.splitext(parts[-1])[0],
        "tracknumber": str(track) if track else None,
    }


def load(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        try:
            return EasyID3(path), "easy"
        except ID3NoHeaderError:
            a = MP3(path)
            a.add_tags()
            a.save()
            return EasyID3(path), "easy"
    if ext in (".m4a", ".mp4", ".aac"):
        return MP4(path), "mp4"
    if ext == ".flac":
        return FLAC(path), "easy"
    if ext == ".ogg":
        return OggVorbis(path), "easy"
    if ext == ".opus":
        return OggOpus(path), "easy"
    return MutagenFile(path, easy=True), "easy"


MP4KEY = {
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "title": "\xa9nam",
    "albumartist": "aART",
}


def get(tags, kind, key):
    if kind == "mp4":
        if key == "tracknumber":
            v = tags.get("trkn")
            return str(v[0][0]) if v else None
        v = tags.get(MP4KEY[key])
        return v[0] if v else None
    v = tags.get(key)
    return v[0] if v else None


def put(tags, kind, key, val):
    if kind == "mp4":
        if key == "tracknumber":
            tags["trkn"] = [(int(val), 0)]
        else:
            tags[MP4KEY[key]] = [val]
    else:
        tags[key] = [val]


def process(path):
    rel = os.path.relpath(path, ROOT)
    try:
        tags, kind = load(path)
    except Exception as e:
        print(f"[skip] {rel}: {e}", flush=True)
        return False
    if tags is None:
        return False

    want = derive(rel)
    changed = []
    for key in ("artist", "album", "title", "tracknumber"):
        if not want[key]:
            continue
        if not get(tags, kind, key):
            put(tags, kind, key, want[key])
            changed.append(key)
    # albumartist keeps Navidrome from splitting compilations into one album per track
    if not get(tags, kind, "albumartist"):
        put(tags, kind, "albumartist", want["artist"])
        changed.append("albumartist")

    if not changed:
        return False
    try:
        tags.save()
    except Exception as e:
        print(f"[fail] {rel}: {e}", flush=True)
        return False
    print(f"[tag] {rel} -> artist={want['artist']!r} album={want['album']!r} title={want['title']!r}", flush=True)
    return True


def sweep():
    n = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            if os.path.splitext(fn)[1].lower() in EXTS:
                if process(os.path.join(dirpath, fn)):
                    n += 1
    return n


if __name__ == "__main__":
    print(f"tagger watching {ROOT} every {INTERVAL}s", flush=True)
    while True:
        try:
            n = sweep()
            if n:
                print(f"tagged {n} file(s)", flush=True)
        except Exception as e:
            print(f"[sweep error] {e}", flush=True)
        time.sleep(INTERVAL)
