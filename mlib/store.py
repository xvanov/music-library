"""Put approved files into the library — either straight onto disk or over WebDAV."""
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


def listing():
    """List library-relative audio paths (direct mode only)."""
    if cfg.mode() != "direct":
        raise StoreError("listing requires direct mode; use `mlib ls` against Navidrome instead")
    root = Path(cfg.music_dir)
    exts = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac"}
    return sorted(
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in exts
    )


def _prune_empty(directory):
    """Remove now-empty artist/album folders after a delete."""
    root = Path(cfg.music_dir).resolve()
    directory = Path(directory).resolve()
    while directory != root and root in directory.parents:
        if any(directory.iterdir()):
            return
        directory.rmdir()
        directory = directory.parent
