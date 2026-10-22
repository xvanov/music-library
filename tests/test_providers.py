"""Providers: the sovmusic parser, URL detection, tag wiping, and the cross-process lock."""
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mlib import catalog, providers, tag  # noqa: E402
from mlib.lock import LockTimeout, file_lock  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _page(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def _by_id(results):
    return {r["id"]: r for r in results}


# -- sovmusic parser -----------------------------------------------------


def test_parse_sovmusic_instrumental_march():
    hits = _by_id(providers.parse_sovmusic(_page("sovmusic_vstrechny.html")))
    assert len(hits) == 7
    r = hits["vstrech3"]
    assert r["title"] == "Встречный марш"
    assert r["performer"] == "Оркестр НКО СССР п/у С.А.Чернецкого"
    assert (r["year"], r["written"], r["vocal"], r["flags"]) == (1936, 1936, False, [])
    assert r["url"] == "http://www.sovmusic.ru/m/vstrech3.mp3"


def test_parse_sovmusic_vocal_and_labels():
    hits = _by_id(providers.parse_sovmusic(_page("sovmusic_katyusha.html")))
    assert len(hits) == 27
    assert hits["katyush5"]["performer"] == "Лидия Русланова" and hits["katyush5"]["vocal"]
    assert hits["katyush1"]["year"] == 1948
    assert hits["katyush7"]["vocal"] is False        # a band, no lyricist credit
    assert hits["katyus20"]["vocal"] is None         # quoted lyrics, no credit
    foreign = {k for k, r in hits.items() if "foreign-language" in r["flags"]}
    assert foreign == {"katyush9", "katyus15", "katyus18", "katyus12", "sovetsk4", "china_19",
                       "katjusch", "katyus13", "katyus14"}
    assert hits["katyus11"]["title"] == "Блантер - Катюша"   # not a language suffix


def test_parse_sovmusic_skips_amateur_section_and_flags_film():
    page = ('<a href=sam_download.php?fname=s13259><b>Катюша</a><br><i>x Комментариев'
            '<a href=download.php?fname=film1><b>Катюша</a><br><i> Музыка: М. Блантер '
            'Слова: М. Исаковский 1938г. Исполняет: из к/ф "Фронтовые подруги" Комментариев')
    hits = providers.parse_sovmusic(page)
    assert [r["id"] for r in hits] == ["film1"]
    assert hits[0]["flags"] == ["film"]


def test_pick_sources_filters_and_ranks():
    song = {"title": "Катюша", "aliases": []}
    results = providers.parse_sovmusic(_page("sovmusic_katyusha.html"))
    kept, dropped = catalog.pick_sources(song, "vocal", results)
    ids = [r["id"] for r in kept]
    assert "frontov2" not in ids                     # "Фронтовая Катюша" is another song
    assert not {"katyush7", "katyus13"} & set(ids)   # band, foreign cover
    assert "katyus11" in ids                         # "Блантер - Катюша"
    assert kept[0]["id"] == "katyush1"               # oldest dated, usual lyrics
    assert ids.index("katyus16") > ids.index("katyush8")   # Червинский parody sorts late
    assert {why for _r, why in dropped} >= {"another song", "foreign-language", "wrong version"}

    march = {"title": "Встречный марш", "aliases": []}
    kept, _ = catalog.pick_sources(march, "instrumental",
                                   providers.parse_sovmusic(_page("sovmusic_vstrechny.html")))
    assert [r["id"] for r in kept] == ["vstrech3"]
    assert catalog.pick_sources(march, "vocal", kept)[0] == []


# -- URL detection -------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("http://www.sovmusic.ru/m/vstrech3.mp3", ("sovmusic", "vstrech3")),
    ("http://sovmusic.ru/m32/vstrech3.mp3", ("sovmusic", "vstrech3")),
    ("http://sovmusic.ru/download.php?fname=vstrech3", ("sovmusic", "vstrech3")),
    ("https://archive.org/details/78_clouds", ("archive.org", "78_clouds")),
    ("https://archive.org/download/78_clouds/01%20Clouds.mp3", ("archive.org", "78_clouds/01 Clouds.mp3")),
    ("https://www.youtube.com/watch?v=abc&t=3", ("youtube", "abc")),
    ("https://example.com/x.mp3", (None, None)),
])
def test_parse_url(url, expected):
    assert providers.parse(url) == expected


def test_detect_and_canonical():
    assert providers.detect("http://sovmusic.ru/download.php?fname=x") == "sovmusic"
    assert providers.detect("https://example.com/x") == "youtube"     # yt-dlp's generic
    assert providers.detect(None) == "youtube"
    assert providers.canonical_url("http://sovmusic.ru/m32/x1.mp3") == \
        "http://www.sovmusic.ru/m/x1.mp3"
    assert catalog.source_id("http://sovmusic.ru/download.php?fname=x1") == \
        ("sovmusic", "sovmusic:x1")
    assert providers.name("archive") == "archive.org"


def test_catalog_stores_canonical_sovmusic_url():
    cat = {"songs": {}}
    _sid, _v, s = catalog.record(cat, "http://sovmusic.ru/m32/vstrech3.mp3", "Встречный марш",
                                 "Оркестр НКО", collection="sov instrumental",
                                 extra={"year": 1936, "flags": None})
    assert s["url"] == "http://www.sovmusic.ru/m/vstrech3.mp3"
    assert (s["provider"], s["year"]) == ("sovmusic", 1936) and "flags" not in s


# -- tags ----------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_wipe_leaves_no_mojibake(tmp_path):
    from mutagen.id3 import COMM, ID3, TIT2, TXXX

    path = tmp_path / "sov.mp3"
    subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "1", str(path)], check=True)
    junk = "Âñòðå÷íûé ìàðø"     # cp1251 read as Latin-1, as sovmusic ships it
    tags = ID3()
    tags.add(TIT2(encoding=0, text=junk))
    tags.add(COMM(encoding=0, lang="rus", desc="", text=junk))
    tags.add(TXXX(encoding=0, desc="info", text=junk))
    tags.save(path, v1=2)
    assert path.read_bytes()[-128:-125] == b"TAG"

    tag.wipe(path)
    tag.apply(path, artist="Оркестр НКО", album="sov instrumental", title="Встречный марш",
              albumartist="Various Artists", compilation=True)
    frames = ID3(path)
    assert not frames.getall("COMM") and not frames.getall("TXXX")
    assert str(frames["TIT2"]) == "Встречный марш"
    assert path.read_bytes()[-128:-125] != b"TAG"
    assert junk not in path.read_bytes().decode("latin-1")


# -- lock ----------------------------------------------------------------


def test_lock_excludes_and_releases(tmp_path):
    lockfile = tmp_path / "x.lock"
    with file_lock(lockfile):
        with pytest.raises(LockTimeout):
            with file_lock(lockfile, timeout=0.3):
                pass
    assert not lockfile.exists()
    with file_lock(lockfile, timeout=0.3):
        pass


def test_lock_breaks_stale(tmp_path):
    lockfile = tmp_path / "x.lock"
    lockfile.write_text("99999")
    old = time.time() - 600
    import os
    os.utime(lockfile, (old, old))
    with file_lock(lockfile, timeout=0.3, stale=120):
        pass


def test_lock_across_processes(tmp_path):
    lockfile = tmp_path / "x.lock"
    root = str(Path(__file__).resolve().parent.parent)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; sys.path.insert(0, {!r}); from mlib.lock import file_lock\n"
        "with file_lock({!r}):\n    print('held', flush=True); time.sleep(1.5)"
    ).format(root, str(lockfile))], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    start = time.time()
    with file_lock(lockfile, timeout=10):
        waited = time.time() - start
    holder.wait()
    assert waited > 0.5


def test_catalog_edit_saves_only_on_success(tmp_path):
    path = tmp_path / "songs.json"
    with catalog.edit(path) as cat:
        catalog.record(cat, "https://youtu.be/a", "Катюша", "Хор", collection="sov vocal")
    with pytest.raises(RuntimeError):
        with catalog.edit(path) as cat:
            cat["songs"].clear()
            raise RuntimeError("boom")
    assert list(catalog.load(path)["songs"]) == ["sov/катюша"]
    assert not path.with_name("songs.json.lock").exists()


def test_a_new_provider_is_one_class(tmp_path, monkeypatch):
    class Bandcamp(providers.Provider):
        name = "bandcamp"

        def parse(self, url):
            u = providers.urlparse(url)
            return u.path.strip("/") or None if (u.hostname or "").endswith("bandcamp.com") \
                else None

        def search(self, query, limit):
            return [{"provider": self.name, "id": "t", "url": "https://x.bandcamp.com/t",
                     "title": query, "vocal": None, "flags": []}]

        def download(self, key, url, dest):
            path = dest / "bandcamp-{}.mp3".format(key.replace("/", "_"))
            path.write_bytes(b"")
            return path, {"id": key}

    monkeypatch.setattr(providers, "REGISTRY", [Bandcamp()] + providers.REGISTRY)
    monkeypatch.setattr(providers.tag, "wipe", lambda path: None)
    url = "https://artist.bandcamp.com/track/song"
    assert providers.detect(url) == "bandcamp"
    assert catalog.source_id(url) == ("bandcamp", "bandcamp:track/song")
    assert providers.search("x", provider="bandcamp")[0]["provider"] == "bandcamp"
    path, _info = providers.download(url, tmp_path)
    assert path.name == "bandcamp-track_song.mp3"
