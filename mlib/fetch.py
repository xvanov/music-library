"""Acquire audio from a URL (or a search query) using yt-dlp.

Only ever writes into staging/ — nothing reaches the library until it is approved.
"""
import json
from pathlib import Path

from .config import cfg, STAGING
from .naming import clean, split_artist_title


def _ydl(opts):
    from yt_dlp import YoutubeDL

    base = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": True,
    }
    base.update(opts)
    return YoutubeDL(base)


def search(query, limit=8):
    """Return candidate tracks for a free-text query, without downloading."""
    results = []
    with _ydl({"skip_download": True, "extract_flat": True}) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        artist, title = split_artist_title(entry.get("title") or "")
        results.append(
            {
                "id": entry.get("id"),
                "url": entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}",
                "raw_title": entry.get("title"),
                "artist": clean(entry.get("uploader") or artist or ""),
                "title": title,
                "duration": entry.get("duration"),
                "channel": entry.get("uploader") or entry.get("channel"),
            }
        )
    return results


def probe(url):
    """Fetch metadata for a single URL without downloading it."""
    with _ydl({"skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return None
    artist, title = split_artist_title(info.get("title") or "")
    return {
        "id": info.get("id"),
        "url": info.get("webpage_url") or url,
        "raw_title": info.get("title"),
        # yt-dlp exposes real music metadata on some extractors; prefer it.
        "artist": clean(info.get("artist") or artist or info.get("uploader") or ""),
        "album": clean(info.get("album") or ""),
        "title": clean(info.get("track") or title or ""),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
    }


def download(url, dest_dir=None):
    """Download one URL to staging as an audio file. Returns the Path written."""
    dest = Path(dest_dir or STAGING)
    dest.mkdir(parents=True, exist_ok=True)
    outtmpl = str(dest / "%(id)s.%(ext)s")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": cfg.audio_format,
                "preferredquality": cfg.audio_quality,
            }
        ],
        "quiet": True,
    }
    with _ydl(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info:
        raise RuntimeError(f"download failed: {url}")

    produced = dest / f"{info['id']}.{cfg.audio_format}"
    if not produced.exists():
        matches = list(dest.glob(f"{info['id']}.*"))
        if not matches:
            raise RuntimeError(f"no output file for {url}")
        produced = matches[0]
    return produced, info


def sidecar_path(audio_path):
    return Path(audio_path).with_suffix(".json")


def write_sidecar(audio_path, meta):
    """Store the intended tags next to the staged file so `approve` can apply them."""
    sidecar_path(audio_path).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_sidecar(audio_path):
    path = sidecar_path(audio_path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
