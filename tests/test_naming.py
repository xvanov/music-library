"""The library layout is a contract between mlib and the server-side tagger."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mlib.naming import clean, relpath, safe, split_artist_title  # noqa: E402


def test_relpath_basic():
    assert relpath("Bijelo Dugme", "balkan", "Bosanska Artiljerija") == \
        "Bijelo Dugme/balkan/Bosanska Artiljerija.mp3"


def test_relpath_track_number_is_zero_padded():
    assert relpath("Nick Cave", "The Boatman's Call", "Into My Arms", 1) == \
        "Nick Cave/The Boatman's Call/01 - Into My Arms.mp3"


def test_relpath_defaults():
    assert relpath(None, None, "Loose Track") == "Unknown Artist/Singles/Loose Track.mp3"


def test_slashes_never_create_directories():
    assert "/" not in safe("AC/DC")


def test_clean_strips_upload_noise():
    assert clean("Song Name (Official Music Video)") == "Song Name"
    assert clean("Song Name [HD]") == "Song Name"


def test_split_artist_title():
    assert split_artist_title("Bijelo Dugme - Bosanska Artiljerija") == \
        ("Bijelo Dugme", "Bosanska Artiljerija")
    assert split_artist_title("JustATitle") == (None, "JustATitle")


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception:
                failures += 1
                print("FAIL", name)
                traceback.print_exc()
    sys.exit(1 if failures else 0)
