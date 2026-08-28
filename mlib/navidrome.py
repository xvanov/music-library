"""Read the live library through Navidrome's Subsonic API."""
import hashlib
import secrets

from .config import cfg


class NavidromeError(RuntimeError):
    pass


def _params():
    if not (cfg.nd_url and cfg.nd_user and cfg.nd_pass):
        raise NavidromeError("set MLIB_ND_URL / MLIB_ND_USER / MLIB_ND_PASS in .env")
    salt = secrets.token_hex(8)
    token = hashlib.md5((cfg.nd_pass + salt).encode("utf-8")).hexdigest()
    return {
        "u": cfg.nd_user,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "mlib",
        "f": "json",
    }


def _call(endpoint, **extra):
    import requests

    params = _params()
    params.update(extra)
    r = requests.get(f"{cfg.nd_url}/rest/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    body = r.json().get("subsonic-response", {})
    if body.get("status") != "ok":
        raise NavidromeError(body.get("error", {}).get("message", "unknown error"))
    return body


def albums(size=200):
    body = _call("getAlbumList2", type="alphabeticalByArtist", size=size)
    return body.get("albumList2", {}).get("album", [])


def tracks(album_id):
    body = _call("getAlbum", id=album_id)
    return body.get("album", {}).get("song", [])


def search(query, limit=20):
    body = _call("search3", query=query, songCount=limit, albumCount=limit, artistCount=limit)
    return body.get("searchResult3", {})


def scan():
    """Ask Navidrome to rescan now instead of waiting for the watcher."""
    return _call("startScan")


def ping():
    _call("ping")
    return True
