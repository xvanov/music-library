"""Podcasts: feed parsing, choosing the next episode, and the heard -> replace cycle."""
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mlib import cli, navidrome, podcasts, tag  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _entries():
    return podcasts.parse_feed((FIXTURES / "podcast_feed.xml").read_bytes())


def _show(**kw):
    return dict(podcasts.new_show("Example Talk Show", feed="https://example.com/rss"), **kw)


def test_parse_feed_newest_first_audio_only():
    eps = _entries()
    assert [e["guid"] for e in eps] == ["ep-trailer", "ep-3", "ep-2", "ep-1"]
    trailer, ep3, ep2, _ep1 = eps
    assert ep3["published"] == "2026-10-24T14:00:00Z"       # -0400 normalised to UTC
    assert ep3["duration"] == 6130 and ep2["duration"] == 3480 and trailer["duration"] == 95
    assert ep3["ext"] == "m4a" and trailer["ext"] == "mp3"   # from type, then from path
    assert podcasts.feed_title((FIXTURES / "podcast_feed.xml").read_bytes()) == \
        "Example Talk Show"


def test_pubdate_accepts_iso_8601():
    assert podcasts._pubdate("Fri, 24 Oct 2026 10:00:00 -0400") == "2026-10-24T14:00:00Z"
    assert podcasts._pubdate("2026-10-24T10:00:00-04:00") == "2026-10-24T14:00:00Z"
    assert podcasts._pubdate("2026-10-24T14:00:00Z") == "2026-10-24T14:00:00Z"
    assert podcasts._pubdate("sometime last week") is None


def test_best_match_skips_a_fan_feed_with_the_same_name():
    results = [
        {"name": "Example Host - Audio Biography", "latest": "2026-10-25", "episodes": 70},
        {"name": "The Example Show with A Host", "latest": "2026-04-02",
         "episodes": 812},                                   # a fan's re-upload feed
        {"name": "The Example Show with A Host", "latest": "2026-10-23", "episodes": 908},
    ]
    assert podcasts.best_match("the example show with a host", results) == 2
    assert podcasts.best_match("Example Show", results) == 0      # no exact name: Apple's first


def test_find_exact_never_takes_a_new_show_for_an_old_one():
    state = {"shows": {"The Example Show": {}, "Example Show Clips": {}}}
    assert podcasts.find(state, "example show", exact=True) == []
    assert podcasts.find(state, "the example show", exact=True) == ["The Example Show"]
    assert sorted(podcasts.find(state, "example show")) == ["Example Show Clips",
                                                             "The Example Show"]


def test_choose_newest_that_passes_filters():
    assert [e["guid"] for e in podcasts.choose(_show(), _entries())] == ["ep-trailer"]
    assert [e["guid"] for e in podcasts.choose(_show(min_minutes=10), _entries())] == ["ep-3"]
    assert [e["guid"] for e in podcasts.choose(_show(title_exclude=r"^trailer"),
                                               _entries())] == ["ep-3"]
    assert [e["guid"] for e in podcasts.choose(_show(keep=2, min_minutes=10),
                                               _entries())] == ["ep-3", "ep-2"]


def test_choose_never_goes_back_past_a_heard_episode():
    show = _show(min_minutes=10, heard=["ep-3"], heard_until="2026-10-24T14:00:00Z")
    assert podcasts.choose(show, _entries()) == []       # ep-2 is older than what was heard
    show["heard_until"] = None                            # the guid alone also stops it
    assert podcasts.choose(show, _entries()) == []


def test_choose_takes_a_second_episode_published_in_the_same_second():
    entries = [dict(e, published="2026-10-24T14:00:00Z") for e in _entries()[1:3]]
    entries[1]["guid"] = "ep-3b"
    show = _show(heard=["ep-3"], heard_until="2026-10-24T14:00:00Z")
    assert [e["guid"] for e in podcasts.choose(show, entries)] == ["ep-3b"]


def test_choose_cuts_undated_entries_at_the_last_heard_or_live_one():
    entries = [{"guid": "yt:" + c, "title": c, "published": None, "duration": None}
               for c in "abcd"]
    show = _show(heard=["yt:c"], heard_until="2026-10-24T14:00:00Z", keep=3)
    assert [e["guid"] for e in podcasts.choose(show, entries)] == ["yt:a", "yt:b"]
    show = _show(live=[{"guid": "yt:b"}], keep=3)
    assert [e["guid"] for e in podcasts.choose(show, entries)] == ["yt:a"]


def test_choose_keeps_an_unheard_live_episode():
    show = _show(min_minutes=10, live=[{"guid": "ep-2", "title": "x", "relpath": "x"}])
    assert podcasts.choose(show, _entries()) == []
    show["keep"] = 2
    assert [e["guid"] for e in podcasts.choose(show, _entries())] == ["ep-3"]


def test_choose_gives_up_on_a_guid_that_keeps_failing():
    show = _show(min_minutes=10, failed={"ep-3": 2})
    assert [e["guid"] for e in podcasts.choose(show, _entries())] == ["ep-3"]
    show["failed"]["ep-3"] = 3
    assert [e["guid"] for e in podcasts.choose(show, _entries())] == ["ep-2"]


def test_owned_refuses_paths_outside_the_show():
    show = _show()
    assert podcasts.owned(show, "Example Talk Show/Example Talk Show/2026-10-24 - X.mp3")
    assert not podcasts.owned(show, "Example Talk Show/Other/X.mp3")
    assert not podcasts.owned(show, "Example Talk Show/Example Talk Show/../../sov/X.mp3")
    assert not podcasts.owned(show, "")


def test_finished_waits_for_the_grace_period_and_skips_now_playing(monkeypatch):
    monkeypatch.setattr(podcasts.cfg, "pod_grace_hours", 6)
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    song = {"id": "s1", "playCount": 1, "played": "2026-10-26T05:59:59.123456789Z"}
    assert podcasts.finished(song, set(), now)
    assert not podcasts.finished(song, {"s1"}, now)                         # playing right now
    assert not podcasts.finished(dict(song, played="2026-10-26T09:00:00Z"), set(), now)
    assert not podcasts.finished(dict(song, played=None), set(), now)       # no time: wait
    assert not podcasts.finished(dict(song, playCount=0), set(), now)
    assert podcasts.finished(dict(song, played="2026-10-26T07:00:00+02:00"), set(), now)


def test_navidrome_path_match_respects_folder_boundaries(monkeypatch):
    songs = [{"title": "t", "album": "a", "path": "XEx/Ex/1.mp3", "playCount": 9},
             {"title": "t", "album": "a", "path": "Ex/Ex/1.mp3", "playCount": 2}]
    monkeypatch.setattr(navidrome, "_call",
                        lambda *a, **k: {"searchResult3": {"song": songs}})
    assert navidrome.find_song("other", "other", "Ex/Ex/1.mp3")["playCount"] == 2


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_raw_aac_is_remuxed_to_a_taggable_m4a(tmp_path):
    path = tmp_path / "ep.aac"
    subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "1", "-c:a", "aac", str(path)], check=True)
    out = podcasts.remux_aac(path)
    assert out.suffix == ".m4a" and not path.exists()
    tag.apply(out, artist="Show", title="t", genre="Podcast")
    assert tag.read(out)["artist"] == "Show"


# -- the sync cycle, with the network, Navidrome and the library mocked ---


class Library:
    """The library as store.py sees it: audio files plus mlib's small text files."""

    def __init__(self):
        self.files, self.texts, self.deleted = set(), {}, []

    def put(self, local, rel):
        self.files.add(rel)
        Path(local).unlink()

    def delete(self, rel):
        self.deleted.append(rel)
        found = rel in self.files
        self.files.discard(rel)
        return found

    def exists(self, rel):
        return rel in self.files

    def read_text(self, rel):
        return self.texts.get(rel)

    def write_text(self, rel, text):
        self.texts[rel] = text


NAME = "Example Talk Show"
EP3_TITLE = "2026-10-24 - Episode 3: Long Talk"
EP3 = NAME + "/" + NAME + "/2026-10-24 - Episode 3- Long Talk.m4a"
LONG_AGO = "2026-01-01T00:00:00Z"


@pytest.fixture
def world(tmp_path, monkeypatch):
    lib = Library()
    songs = {}          # title -> Subsonic song, as Navidrome would report it
    nd = {"up": True, "playing": set()}
    feed = {"entries": _entries()}
    downloads = []

    def now_playing():
        if not nd["up"]:
            raise ConnectionError("unreachable")
        return nd["playing"]

    def download(ep, dest=None, keepalive=None):
        downloads.append(ep["guid"])
        local = tmp_path / "{}.{}".format(ep["guid"], ep["ext"])
        local.write_bytes(b"audio")
        return local

    for name in ("put", "delete", "exists", "read_text", "write_text"):
        monkeypatch.setattr(podcasts.store, name, getattr(lib, name))
    monkeypatch.setattr(podcasts.navidrome, "now_playing", now_playing)
    monkeypatch.setattr(podcasts.navidrome, "find_song",
                        lambda title, album, path=None: songs.get(title))
    monkeypatch.setattr(podcasts.tag, "wipe", lambda p: None)
    monkeypatch.setattr(podcasts.tag, "apply", lambda p, **kw: [])
    monkeypatch.setattr(podcasts, "download_episode", download)
    monkeypatch.setattr(podcasts, "fetch_entries", lambda show: feed["entries"])
    monkeypatch.setattr(podcasts, "BACKUP", tmp_path / "catalog" / "podcasts.json")
    monkeypatch.setattr(podcasts, "EDIT_LOCK", tmp_path / "locks" / "podcasts.lock")
    monkeypatch.setattr(podcasts, "SYNC_LOCK", tmp_path / "locks" / "podcasts.sync.lock")
    (tmp_path / "catalog").mkdir()
    podcasts.save({"shows": {NAME: _show(min_minutes=10)}})

    class World:
        pass

    w = World()
    w.lib, w.songs, w.nd, w.feed, w.downloads = lib, songs, nd, feed, downloads
    w.backup = tmp_path / "catalog" / "podcasts.json"
    w.sync = lambda names=None: podcasts.sync(names, log=lambda *a: None)
    w.show = lambda: podcasts.load()["shows"][NAME]

    def played(title, count=1, at=LONG_AGO):
        songs[title] = {"id": title, "title": title, "playCount": count, "played": at}

    w.played = played
    return w


def test_first_sync_publishes_the_newest_episode(world):
    assert world.sync() == (1, [])
    assert world.lib.files == {EP3}
    live = world.show()["live"]
    assert [e["guid"] for e in live] == ["ep-3"]
    assert live[0]["title"] == EP3_TITLE


def test_state_lives_in_the_library_with_a_catalog_backup(world):
    world.sync()
    shared = json.loads(world.lib.texts[podcasts.STATE])
    assert [e["guid"] for e in shared["shows"][NAME]["live"]] == ["ep-3"]
    assert json.loads(world.backup.read_text(encoding="utf-8")) == shared


def test_first_run_migrates_the_catalog_copy_into_the_library(world):
    world.lib.texts.clear()
    assert world.sync() == (1, [])
    assert podcasts.STATE in world.lib.texts


def test_sync_with_no_state_or_no_shows_fails_loudly(world):
    world.lib.texts.clear()
    world.backup.unlink()
    with pytest.raises(podcasts.PodcastError):
        world.sync()
    podcasts.save({"shows": {}})
    with pytest.raises(podcasts.PodcastError):
        world.sync()


def test_another_machine_s_change_is_not_overwritten(world):
    world.sync()
    shared = json.loads(world.lib.texts[podcasts.STATE])
    shared["shows"]["Another Show"] = _show(name="Another Show", live=[{"guid": "x"}])
    world.lib.texts[podcasts.STATE] = json.dumps(shared)     # written by the server meanwhile
    world.played(EP3_TITLE)
    world.sync([NAME])
    assert "Another Show" in podcasts.load()["shows"]


def test_unheard_episode_is_kept(world):
    world.sync()
    world.played(EP3_TITLE, count=0)
    assert world.sync() == (0, [])
    assert world.lib.files == {EP3} and world.lib.deleted == []
    assert world.downloads == ["ep-3"]


def test_heard_episode_is_deleted_and_replaced_by_a_newer_one(world):
    world.sync()
    world.played(EP3_TITLE)
    world.feed["entries"] = [dict(world.feed["entries"][1], guid="ep-4", title="Episode 4",
                                  published="2026-10-31T10:00:00Z")] + world.feed["entries"]
    assert world.sync() == (2, [])
    assert world.lib.deleted == [EP3]
    show = world.show()
    assert [e["guid"] for e in show["live"]] == ["ep-4"]
    assert show["heard"] == ["ep-3"] and show["heard_until"] == "2026-10-24T14:00:00Z"


def test_a_recent_scrobble_is_not_yet_heard(world):
    world.sync()
    world.played(EP3_TITLE, at=datetime.now(timezone.utc).isoformat())   # halfway, just now
    assert world.sync() == (0, [])
    assert world.lib.deleted == []


def test_an_episode_playing_now_is_kept(world):
    world.sync()
    world.played(EP3_TITLE)
    world.nd["playing"] = {EP3_TITLE}
    assert world.sync() == (0, [])
    assert world.lib.deleted == []


def test_heard_with_nothing_newer_waits_and_never_refetches(world):
    world.sync()
    world.played(EP3_TITLE, count=2)
    assert world.sync() == (1, [])                # deleted, nothing to replace it with
    assert world.show()["live"] == []
    assert world.sync() == (0, [])
    assert world.downloads == ["ep-3"]            # neither ep-3 again nor the older ep-2


def test_navidrome_unreachable_deletes_nothing(world):
    world.sync()
    world.played(EP3_TITLE, count=5)
    world.nd["up"] = False
    assert world.sync() == (0, [])
    assert world.lib.deleted == [] and world.lib.files == {EP3}
    assert [e["guid"] for e in world.show()["live"]] == ["ep-3"]


def test_track_not_yet_scanned_is_kept(world):
    world.sync()
    assert world.sync() == (0, [])                # not in Navidrome, but the file is there
    assert world.lib.deleted == [] and world.show()["live"]


def test_a_vanished_file_counts_as_heard_and_frees_the_slot(world):
    world.sync()
    world.lib.files.clear()                       # deleted by hand, and rescanned away
    world.feed["entries"] = [dict(world.feed["entries"][1], guid="ep-4", title="Episode 4",
                                  published="2026-10-31T10:00:00Z")] + world.feed["entries"]
    world.sync()
    show = world.show()
    assert show["heard"] == ["ep-3"] and [e["guid"] for e in show["live"]] == ["ep-4"]


def test_never_deletes_outside_the_show_folder(world):
    world.sync()
    state = podcasts.load()
    state["shows"][NAME]["live"][0]["relpath"] = "sov vocal/Хор - Катюша.mp3"
    podcasts.save(state)
    world.lib.files.add("sov vocal/Хор - Катюша.mp3")
    world.played(EP3_TITLE)
    world.sync()
    assert world.lib.deleted == [] and world.show()["live"]


def test_name_collision_gets_a_guid_hash(world):
    world.lib.files.add(EP3)                      # another episode already took the name
    world.sync()
    (path,) = world.lib.files - {EP3}
    assert path.startswith(NAME + "/" + NAME + "/2026-10-24 - Episode 3- Long Talk [")


def test_long_titles_are_cut_so_the_date_survives(world):
    world.feed["entries"] = [dict(world.feed["entries"][1], title="Word " * 60)]
    world.sync()
    (path,) = world.lib.files
    stem = Path(path).stem
    assert stem.startswith("2026-10-24 - Word") and len(stem) <= 120


def test_failed_download_is_counted_raised_and_given_up_after_three(world, monkeypatch):
    tried = []

    def broken(ep, dest=None, keepalive=None):
        tried.append(ep["guid"])
        raise IOError("connection reset")

    monkeypatch.setattr(podcasts, "download_episode", broken)
    for _ in range(3):
        assert world.sync() == (0, [NAME])        # never reaches for the older ep-2 meanwhile
    assert tried == ["ep-3"] * 3 and world.show()["failed"] == {"ep-3": 3}
    monkeypatch.setattr(podcasts, "download_episode", lambda ep, **kw: tried.append(
        ep["guid"]) or (world.backup.parent / "x.mp3").write_bytes(b"a") or
        world.backup.parent / "x.mp3")
    world.sync()
    assert tried[-1] == "ep-2"


def test_one_failing_show_does_not_stop_the_others(world, monkeypatch):
    state = podcasts.load()
    state["shows"]["Broken Show"] = _show(name="Broken Show", feed="https://broken")
    podcasts.save(state)

    def entries(show):
        if show["name"] == "Broken Show":
            raise IOError("feed is gone")
        return world.feed["entries"]

    monkeypatch.setattr(podcasts, "fetch_entries", entries)
    assert world.sync() == (1, ["Broken Show"])
    assert world.lib.files == {EP3}


def test_a_show_removed_mid_sync_does_not_keep_its_new_file(world, monkeypatch):
    real = podcasts.publish

    def publish_then_unsubscribe(show, ep, path):
        target = real(show, ep, path)
        with podcasts.edit() as state:
            del state["shows"][NAME]
        return target

    monkeypatch.setattr(podcasts, "publish", publish_then_unsubscribe)
    world.sync()
    assert world.lib.files == set() and world.lib.deleted == [EP3]
    assert podcasts.load()["shows"] == {}


def test_cli_sync_exits_nonzero_when_a_show_fails(world, monkeypatch, capsys):
    monkeypatch.setattr(podcasts, "fetch_entries", lambda show: (_ for _ in ()).throw(
        IOError("down")))
    monkeypatch.setattr(cli, "_rescan", lambda: None)
    assert cli.main(["pod", "sync"]) == 1
    world.lib.texts.clear()
    world.backup.unlink()
    assert cli.main(["pod", "sync"]) == 1
    assert "no podcast subscriptions" in capsys.readouterr().err


def test_cli_rm_takes_the_sync_lock_and_deletes_only_its_files(world, monkeypatch):
    monkeypatch.setattr(cli, "_rescan", lambda: None)
    world.sync()
    assert cli.main(["pod", "rm", NAME]) == 0
    assert world.lib.deleted == [EP3] and podcasts.load()["shows"] == {}
    assert not podcasts.SYNC_LOCK.exists()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.parametrize("ext", ["mp3", "m4a"])
def test_episode_tags_carry_the_podcast_genre(tmp_path, ext):
    path = tmp_path / ("ep." + ext)
    subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "1", str(path)], check=True)
    tag.wipe(path)
    tag.apply(path, artist="Show", album="Show", title="2026-10-24 - Ep", genre="Podcast")
    tags, kind = tag._open(path)
    assert tag._get(tags, kind, "genre") == "Podcast"
    assert tag.read(path)["title"] == "2026-10-24 - Ep"


def test_store_text_round_trip_in_direct_mode(tmp_path, monkeypatch):
    from mlib import store

    monkeypatch.setattr(store.cfg, "music_dir", str(tmp_path))
    assert store.read_text(podcasts.STATE) is None
    store.write_text(podcasts.STATE, '{"shows": {"Ш": 1}}')
    store.write_text(podcasts.STATE, '{"shows": {"Ш": 2}}')
    assert json.loads(store.read_text(podcasts.STATE)) == {"shows": {"Ш": 2}}
    assert [p.name for p in (tmp_path / ".mlib").iterdir()] == ["podcasts.json"]
    assert store.listing() == []                  # not audio: never part of the library
