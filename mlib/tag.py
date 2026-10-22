"""Write ID3/MP4/Vorbis tags. Mirrors the server-side tagger's field choices."""
from pathlib import Path

MP4KEY = {"artist": "\xa9ART", "album": "\xa9alb", "title": "\xa9nam", "albumartist": "aART"}


def _open(path):
    from mutagen.easyid3 import EasyID3
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3NoHeaderError
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    ext = Path(path).suffix.lower()
    if ext == ".mp3":
        try:
            return EasyID3(path), "easy"
        except ID3NoHeaderError:
            audio = MP3(path)
            audio.add_tags()
            audio.save()
            return EasyID3(path), "easy"
    if ext in (".m4a", ".mp4", ".aac"):
        return MP4(path), "mp4"
    if ext == ".flac":
        return FLAC(path), "easy"
    if ext == ".ogg":
        return OggVorbis(path), "easy"
    if ext == ".opus":
        return OggOpus(path), "easy"
    raise ValueError(f"unsupported audio format: {ext}")


def apply(path, artist=None, album=None, title=None, track=None, albumartist=None, overwrite=True,
          compilation=False):
    """Set tags on a file. With overwrite=False, only fills fields that are empty."""
    tags, kind = _open(path)
    if compilation:
        tags["cpil" if kind == "mp4" else "compilation"] = [True] if kind == "mp4" else ["1"]
    values = {
        "artist": artist,
        "album": album,
        "title": title,
        "albumartist": albumartist or artist,
        "tracknumber": str(track) if track else None,
    }
    written = []
    for key, val in values.items():
        if not val:
            continue
        if not overwrite and _get(tags, kind, key):
            continue
        _set(tags, kind, key, val)
        written.append(key)
    tags.save()
    return written


def wipe(path):
    """Remove every tag: ID3v1, ID3v2 (comments, TXXX, pictures...), APEv2, Vorbis.

    sovmusic.ru files carry cp1251 text mislabelled as Latin-1; none of it may
    survive under the tags mlib writes."""
    import mutagen
    from mutagen import apev2, id3

    if Path(path).suffix.lower() == ".mp3":
        id3.delete(path, delete_v1=True, delete_v2=True)
        apev2.delete(path)
        return
    audio = mutagen.File(path)
    if audio is None:
        return
    if hasattr(audio, "clear_pictures"):
        audio.clear_pictures()
        audio.save()
    audio.delete()


def read(path):
    tags, kind = _open(path)
    return {k: _get(tags, kind, k) for k in ("artist", "album", "title", "albumartist", "tracknumber")}


def _get(tags, kind, key):
    if kind == "mp4":
        if key == "tracknumber":
            v = tags.get("trkn")
            return str(v[0][0]) if v else None
        v = tags.get(MP4KEY[key])
        return v[0] if v else None
    v = tags.get(key)
    return v[0] if v else None


def _set(tags, kind, key, val):
    if kind == "mp4":
        if key == "tracknumber":
            tags["trkn"] = [(int(val), 0)]
        else:
            tags[MP4KEY[key]] = [val]
    else:
        tags[key] = [val]
