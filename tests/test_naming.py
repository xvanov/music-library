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


def test_normalize_keeps_non_latin_scripts():
    from mlib.batch import normalize
    # Stripping to [a-z0-9] used to erase these entirely, so every Cyrillic
    # title normalized to "" and scored meaninglessly against candidates.
    assert normalize("Прощание славянки") == "прощание славянки"
    assert normalize("Djurdjevdan!") == "djurdjevdan"


def test_score_discriminates_cyrillic():
    from mlib.batch import score
    entry = {"artist": None, "title": "Прощание славянки", "raw": "x", "album": "Singles"}
    good = {"raw_title": "Прощание славянки 1941", "channel": "Оркестр", "duration": 200}
    bad = {"raw_title": "Katyusha Red Army Choir", "channel": "Music", "duration": 200}
    assert score(entry, good) > 0.6
    assert score(entry, bad) < 0.2


def test_scrub_strips_list_decoration():
    from mlib.batch import parse_list
    entries, bad = parse_list('1. Djurdjevdan\n2) "Kalasnjikov"\n- Iag Bari\n')
    assert not bad
    assert [e["title"] for e in entries] == ["Djurdjevdan", "Kalasnjikov", "Iag Bari"]
    assert all(e["artist"] is None for e in entries)


def test_scrub_strips_chapter_timestamps():
    from mlib.batch import scrub
    # Tracklists copied from a video description lead with chapter timestamps.
    assert scrub("0:00 Aвимарш СССР.") == "Aвимарш СССР"
    assert scrub("1:02:33 - Some Song") == "Some Song"
    assert scrub("4:43 В Путь!") == "В Путь!"


def test_scrub_keeps_leading_year():
    from mlib.batch import scrub
    assert scrub("1984 Overture") == "1984 Overture"


def test_coverage_ignores_channel_name():
    from mlib.batch import score
    # The channel "Подводный флот России" once supplied the missing word for the
    # query "Флот Комсомол", scoring a submarine documentary at 0.95.
    entry = {"artist": None, "title": "Флот Комсомол", "raw": "x", "album": "Singles"}
    doc = {
        "raw_title": "Легендарная субмарина Ленинский комсомол вышла в поход",
        "channel": "Russian Submarine Fleet - Подводный флот России",
        "duration": 200,
    }
    assert score(entry, doc) < 0.6


def test_coverage_rescues_verbose_titles():
    from mlib.batch import score
    # Bilingual and parenthesised titles carry extra words; sequence ratio alone
    # rated these correct matches below threshold.
    entry = {
        "artist": None,
        "title": "Вход Красной Армии в Будапешт",
        "raw": "x",
        "album": "Singles",
    }
    good = {
        "raw_title": 'Марш "Вступление Красной Армии в Будапешт" (Семён Чернецкий)',
        "channel": "",
        "duration": 200,
    }
    assert score(entry, good) > 0.62


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
