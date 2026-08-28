"""Filename and folder conventions.

The library layout is  <Artist>/<Album>/<NN - Title>.<ext>  and the server-side
tagger derives tags from exactly this shape, so both sides must agree. Keep this
module in sync with server/tagger/tagger.py.
"""
import re
import unicodedata

# Characters that are illegal on Windows/SMB or that confuse shells.
_ILLEGAL = re.compile(r'[<>:"/\|?*\x00-\x1f]')
_NOISE = re.compile(
    r"\s*[\(\[]\s*(official\s*)?(music\s*)?"
    r"(video|audio|lyrics?|lyric video|visualizer|hd|hq|4k|full album|remaster(ed)?)"
    r"[^\)\]]*[\)\]]\s*",
    re.I,
)
_SPLIT = re.compile(r"\s+[-–—]\s+")


def clean(text, keep_case=True):
    """Normalize a title/artist string pulled from a source that is often messy."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", str(text))
    text = _NOISE.sub(" ", text)
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return text if keep_case else text.lower()


def safe(component):
    """Make one path component safe to write on any filesystem."""
    component = _ILLEGAL.sub("-", clean(component))
    component = component.strip(" .")
    return (component or "Unknown")[:120]


def split_artist_title(text):
    """Split 'Artist - Title' into its parts; returns (None, text) when absent."""
    text = clean(text)
    bits = _SPLIT.split(text, maxsplit=1)
    if len(bits) == 2 and bits[0].strip() and bits[1].strip():
        return clean(bits[0]), clean(bits[1])
    return None, text


def relpath(artist, album, title, track=None, ext="mp3"):
    """Build the library-relative path for a track."""
    artist = safe(artist or "Unknown Artist")
    album = safe(album or "Singles")
    title = safe(title or "Untitled")
    stem = f"{int(track):02d} - {title}" if track else title
    return f"{artist}/{album}/{safe(stem)}.{ext.lstrip('.')}"
