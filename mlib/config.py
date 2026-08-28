"""Configuration, loaded from the environment (optionally seeded by a .env file)."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGING = REPO_ROOT / "staging"


def _load_dotenv():
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv()


class Config:
    music_dir = os.environ.get("MLIB_MUSIC_DIR") or None
    dav_url = (os.environ.get("MLIB_DAV_URL") or "").rstrip("/")
    dav_user = os.environ.get("MLIB_DAV_USER", "")
    dav_pass = os.environ.get("MLIB_DAV_PASS", "")
    nd_url = (os.environ.get("MLIB_ND_URL") or "").rstrip("/")
    nd_user = os.environ.get("MLIB_ND_USER", "")
    nd_pass = os.environ.get("MLIB_ND_PASS", "")
    audio_format = os.environ.get("MLIB_AUDIO_FORMAT", "mp3")
    audio_quality = os.environ.get("MLIB_AUDIO_QUALITY", "192")

    @classmethod
    def mode(cls):
        """'direct' when we can write to the library folder, else 'webdav'."""
        if cls.music_dir and Path(cls.music_dir).is_dir():
            return "direct"
        if cls.dav_url:
            return "webdav"
        return "none"

    @classmethod
    def describe(cls):
        m = cls.mode()
        if m == "direct":
            return f"direct -> {cls.music_dir}"
        if m == "webdav":
            return f"webdav -> {cls.dav_url}"
        return "NOT CONFIGURED (copy .env.example to .env)"


cfg = Config
