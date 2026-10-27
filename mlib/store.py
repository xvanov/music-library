"""Put approved files into the library — either straight onto disk or over WebDAV."""
import os
import shutil
from pathlib import Path
from urllib.parse import quote

from .config import cfg


class StoreError(RuntimeError):
    pass


def _dav_session():
    import requests
    from requests.auth import HTTPBasicAuth

    s = requests.Session()
    s.auth = HTTPBasicAuth(cfg.dav_user, cfg.dav_pass)
    return s


def _dav_url(relpath):
    return f"{cfg.dav_url}/{quote(relpath)}"


def _dav_mkcol(session, relpath):
    """WebDAV has no mkdir -p, so create each parent in turn."""
    parts = Path(relpath).parent.parts
    for i in range(len(parts)):
        prefix = "/".join(parts[: i + 1])
        r = session.request("MKCOL", _dav_url(prefix))
        # 201 created, 405 already exists — both fine.
        if r.status_code not in (201, 405, 301, 200):
            raise StoreError(f"MKCOL {prefix} -> {r.status_code}")


def exists(relpath):
    if cfg.mode() == "direct":
        return (Path(cfg.music_dir) / relpath).exists()
    session = _dav_session()
    return session.head(_dav_url(relpath)).status_code == 200


def put(local_path, relpath):
    """Store one file at library-relative `relpath`. Returns a human-readable target."""
    mode = cfg.mode()
    if mode == "none":
        raise StoreError("no destination configured; copy .env.example to .env")

    if mode == "direct":
        target = Path(cfg.music_dir) / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(local_path), str(target))
        return str(target)

    session = _dav_session()
    _dav_mkcol(session, relpath)
    with open(local_path, "rb") as fh:
        r = session.put(_dav_url(relpath), data=fh)
    if r.status_code not in (200, 201, 204):
        raise StoreError(f"PUT {relpath} -> {r.status_code} {r.text[:200]}")
    Path(local_path).unlink(missing_ok=True)
    return _dav_url(relpath)


def get(relpath, local_path):
    """Copy one library file to `local_path`, for comparing audio against it."""
    if cfg.mode() == "direct":
        shutil.copyfile(Path(cfg.music_dir) / relpath, local_path)
        return local_path
    r = _dav_session().get(_dav_url(relpath))
    if r.status_code != 200:
        raise StoreError(f"GET {relpath} -> {r.status_code}")
    Path(local_path).write_bytes(r.content)
    return local_path


def read_text(relpath):
    """A small text file in the library (mlib's own state), or None if missing."""
    if cfg.mode() == "direct":
        path = Path(cfg.music_dir) / relpath
        return path.read_text(encoding="utf-8") if path.exists() else None
    r = _dav_session().get(_dav_url(relpath))
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise StoreError(f"GET {relpath} -> {r.status_code}")
    return r.content.decode("utf-8")


def write_text(relpath, text):
    """Replace a small text file in the library - atomically on disk."""
    mode = cfg.mode()
    if mode == "none":
        raise StoreError("no destination configured; copy .env.example to .env")
    data = text.encode("utf-8")
    if mode == "direct":
        target = Path(cfg.music_dir) / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return
    session = _dav_session()
    _dav_mkcol(session, relpath)
    r = session.put(_dav_url(relpath), data=data)
    if r.status_code not in (200, 201, 204):
        raise StoreError(f"PUT {relpath} -> {r.status_code} {r.text[:200]}")


def delete(relpath):
    mode = cfg.mode()
    if mode == "direct":
        target = Path(cfg.music_dir) / relpath
        if not target.exists():
            return False
        target.unlink()
        _prune_empty(target.parent)
        return True

    session = _dav_session()
    r = session.delete(_dav_url(relpath))
    if r.status_code in (200, 204, 404):
        return r.status_code != 404
    raise StoreError(f"DELETE {relpath} -> {r.status_code}")


AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac"}


def listing():
    """List library-relative audio paths, from disk or over WebDAV."""
    if cfg.mode() == "webdav":
        return sorted(p for p in _dav_walk() if Path(p).suffix.lower() in AUDIO_EXTS)
    if cfg.mode() != "direct":
        raise StoreError("no destination configured; copy .env.example to .env")
    root = Path(cfg.music_dir)
    exts = AUDIO_EXTS
    return sorted(
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in exts
    )


def _dav_walk(rel=""):
    """Every file path under `rel`, one PROPFIND per folder."""
    import xml.etree.ElementTree as ET
    from urllib.parse import unquote, urlparse

    base = urlparse(cfg.dav_url).path.rstrip("/")
    r = _dav_session().request("PROPFIND", _dav_url(rel).rstrip("/") + "/", headers={"Depth": "1"})
    if r.status_code not in (200, 207):
        raise StoreError(f"PROPFIND {rel or '/'} -> {r.status_code}")
    out = []
    for resp in ET.fromstring(r.content).findall("{DAV:}response"):
        href = unquote(urlparse(resp.find("{DAV:}href").text).path)[len(base):].strip("/")
        if href == rel.strip("/"):
            continue
        if resp.find(".//{DAV:}collection") is not None:
            out += _dav_walk(href)
        else:
            out.append(href)
    return out


def _prune_empty(directory):
    """Remove now-empty artist/album folders after a delete."""
    root = Path(cfg.music_dir).resolve()
    directory = Path(directory).resolve()
    while directory != root and root in directory.parents:
        if any(directory.iterdir()):
            return
        directory.rmdir()
        directory = directory.parent
