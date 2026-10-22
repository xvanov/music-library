"""The catalog keeps songs apart from sources: merges, verdicts and the replace swap."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mlib import catalog  # noqa: E402

YT = "https://www.youtube.com/watch?v="


def _empty():
    return {"songs": {}}


def test_norm_is_conservative():
    assert catalog.norm("Мы — красные кавалеристы!") == "мы красные кавалеристы"
    assert catalog.norm("Ёлочка") == catalog.norm("елочка")
    assert catalog.norm("Марш В боевой поход") != catalog.norm("В боевой поход")


def test_source_id_survives_url_decoration():
    assert catalog.source_id(YT + "abc&t=30s") == ("youtube", "yt:abc")
    assert catalog.source_id("https://youtu.be/abc") == ("youtube", "yt:abc")
    provider, sid = catalog.source_id("https://www.sovmusic.ru/download.php?fname=x")
    assert provider == "sovmusic" and sid.startswith("sovmusic:")


def test_split_collection():
    assert catalog.split_collection("sov vocal") == ("sov", "vocal")
    assert catalog.split_collection("cossack instrumental") == ("cossack", "instrumental")
    assert catalog.split_collection("balkan") == ("other", None)


def test_versions_of_one_song_share_an_entry():
    cat = _empty()
    catalog.record(cat, YT + "a", "Катюша", "Лидия Русланова", collection="sov vocal")
    catalog.record(cat, YT + "b", "Катюша!", "Оркестр", collection="sov instrumental")
    assert list(cat["songs"]) == ["sov/катюша"]
    assert set(cat["songs"]["sov/катюша"]["versions"]) == {"vocal", "instrumental"}


def test_import_is_idempotent_and_never_moves_backwards():
    listing = ["sov vocal/Хор - Катюша.mp3", "balkan/Bijelo Dugme - Bosanska Artiljerija.mp3"]
    history = [
        {"artist": "Хор", "title": "Катюша", "url": YT + "live1", "status": "live",
         "info": "sov vocal"},
        {"artist": "Капелла", "title": "Катюша", "url": YT + "bad1", "status": "rejected",
         "info": "speech: news report"},
    ]
    sidecars = [{"artist": "Оркестр", "album": "sov instrumental", "title": "Катюша",
                 "source": YT + "stg1", "collection": True,
                 "check": {"verdict": "speech", "detail": "0.9 words/s"}}]
    # a pick for an already-live source must not demote it
    picks = [{"url": YT + "live1", "artist": "Хор", "title": "Катюша", "collection": "sov vocal"},
             {"url": YT + "cand1", "artist": "Ансамбль", "title": "Смуглянка",
              "collection": "sov vocal"}]
    cat = _empty()
    for _ in range(2):
        catalog.import_all(cat, listing, history, sidecars, picks)

    song = cat["songs"]["sov/катюша"]
    vocal = song["versions"]["vocal"]
    assert [s["status"] for s in vocal["sources"]] == ["live", "rejected"]
    assert vocal["status"] == "live"
    assert vocal["library_path"] == "sov vocal/Хор - Катюша.mp3"
    instr = song["versions"]["instrumental"]
    assert instr["status"] == "staged"
    assert instr["sources"][0]["reason"].startswith("held: speech")
    assert cat["songs"]["sov/смуглянка"]["versions"]["vocal"]["status"] == "wanted"
    assert cat["songs"]["other/bosanska-artiljerija"]["versions"]["vocal"]["status"] == "live"


def test_import_syncs_with_the_library_and_staging():
    cat = _empty()
    sidecar = {"artist": "X", "album": "sov vocal", "title": "Песня", "source": YT + "s",
               "collection": True}
    catalog.import_all(cat, ["sov vocal/X - Другая.mp3"], sidecars=[sidecar])
    # the file left the library, the staged file left staging
    catalog.import_all(cat, [], sidecars=[])
    gone = cat["songs"]["sov/другая"]["versions"]["vocal"]
    assert gone["status"] == "removed"
    left = cat["songs"]["sov/песня"]["versions"]["vocal"]["sources"][0]
    assert left["status"] == "candidate"


def test_aliases_merge_and_near_matches_are_reported():
    cat = _empty()
    catalog.record(cat, YT + "1", "Марш В боевой поход", "Оркестр",
                   collection="sov instrumental")
    catalog.record(cat, YT + "2", "В боевой поход", "Хор", collection="sov vocal")
    catalog.record(cat, YT + "3", "Марш танкистов", "Хор", collection="sov vocal")
    assert ("sov/в-боевой-поход", "sov/марш-в-боевой-поход") in catalog.near_matches(cat)

    cat["songs"]["sov/в-боевой-поход"]["aliases"].append("Марш В боевой поход")
    assert catalog.merge_aliases(cat) == ["sov/в-боевой-поход <- sov/марш-в-боевой-поход"]
    assert set(cat["songs"]["sov/в-боевой-поход"]["versions"]) == {"vocal", "instrumental"}
    assert catalog.near_matches(cat) == []
    # and a new source with the alias title lands on the merged song
    sid, _v, _s = catalog.record(cat, YT + "4", "Марш В боевой поход", "Другой оркестр",
                                 collection="sov instrumental")
    assert sid == "sov/в-боевой-поход"


def test_stage_publish_reject_remove():
    cat = _empty()
    meta = {"artist": "Хор", "album": "sov vocal", "title": "Варяг", "source": YT + "v1",
            "collection": True}
    ids = catalog.stage(cat, meta)
    assert ids == {"song": "sov/варяг", "version": "vocal", "source_id": "yt:v1"}
    meta.update(ids)
    catalog.published(cat, meta, "sov vocal/Хор - Варяг.mp3")
    v = cat["songs"]["sov/варяг"]["versions"]["vocal"]
    assert (v["status"], v["current"]) == ("live", "yt:v1")

    bad = {"artist": "Y", "album": "sov vocal", "title": "Варяг", "source": YT + "v2",
           "collection": True, "check": {"verdict": "speech", "detail": "documentary"}}
    catalog.stage(cat, bad)
    catalog.rejected(cat, bad, catalog.check_reason(bad))
    assert catalog.blocked(cat, YT + "v2")["reason"] == "speech: documentary"
    assert v["status"] == "live"

    assert catalog.removed(cat, "sov vocal/Хор - Варяг.mp3", "off-theme") == "sov/варяг"
    assert v["status"] == "removed" and v["library_path"] is None
    assert catalog.blocked(cat, YT + "v1")["status"] == "removed"


def test_replace_swaps_current_source():
    cat = _empty()
    old = {"artist": "Хор", "album": "sov vocal", "title": "Варяг", "source": YT + "old",
           "collection": True}
    catalog.stage(cat, old)
    catalog.published(cat, old, "sov vocal/Хор - Варяг.mp3")
    catalog.record(cat, YT + "new", "Варяг", "Ансамбль", collection="sov vocal")
    v = cat["songs"]["sov/варяг"]["versions"]["vocal"]
    assert catalog.next_candidate(v)["id"] == "yt:new"

    new = {"artist": "Ансамбль", "album": "sov vocal", "title": "Варяг", "source": YT + "new",
           "collection": True, "song": "sov/варяг", "version": "vocal", "source_id": "yt:new",
           "replaces": {"source_id": "yt:old", "library_path": "sov vocal/Хор - Варяг.mp3",
                        "reason": "bad audio"}}
    replaced = catalog.published(cat, new, "sov vocal/Ансамбль - Варяг.mp3")
    assert replaced["id"] == "yt:old" and replaced["reason"] == "bad audio"
    assert [s["status"] for s in v["sources"]] == ["replaced", "live"]
    assert v["library_path"] == "sov vocal/Ансамбль - Варяг.mp3"
    assert catalog.blocked(cat, YT + "old")


def test_save_round_trips(tmp_path):
    cat = _empty()
    catalog.record(cat, YT + "a", "Катюша", "Хор", collection="sov vocal")
    path = tmp_path / "songs.json"
    catalog.save(cat, path)
    assert "Катюша" in path.read_text(encoding="utf-8")
    assert catalog.load(path) == cat
