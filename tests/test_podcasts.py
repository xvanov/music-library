"""Podcasts: feed parsing, choosing the next episode, and the heard -> replace cycle."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mlib import podcasts, tag  # noqa: E402

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


def test_best_match_skips_a_fan_feed_with_the_same_name():
    results = [
        {"name": "Example Host - Audio Biography", "latest": "2026-10-25", "episodes": 70},
        {"name": "The Example Show with A Host", "latest": "2026-04-02",
         "episodes": 812},                                   # a fan's re-upload feed
        {"name": "The Example Show with A Host", "latest": "2026-10-23", "episodes": 908},
    ]
    assert podcasts.best_match("the example show with a host", results) == 2
    assert podcasts.best_match("Example Show", results) == 0      # no exact name: Apple's first


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


def test_choose_keeps_an_unheard_live_episode():
    show = _show(min_minutes=10, live=[{"guid": "ep-2", "title": "x", "relpath": "x"}])
    assert podcasts.choose(show, _entries()) == []
    show["keep"] = 2
    assert [e["guid"] for e in podcasts.choose(show, _entries())] == ["ep-3"]


# -- the sync cycle, with the network, Navidrome and the library mocked ---


class Library:
    def __init__(self):
        self.files, self.deleted = set(), []

    def put(self, local, rel):
        self.files.add(rel)
        Path(local).unlink()

    def delete(self, rel):
        self.deleted.append(rel)
        found = rel in self.files
        self.files.discard(rel)
        return found


@pytest.fixture
def world(tmp_path, monkeypatch):
    lib = Library()
    plays = {}
    nd = {"up": True}
    feed = {"entries": _entries()}
    downloads = []

    def ping():
        if not nd["up"]:
            raise ConnectionError("unreachable")
        return True

    def play_count(title, album, path=None):
        return plays.get(title)

    def download(ep, dest=None, keepalive=None):
        downloads.append(ep["guid"])
        local = tmp_path / "{}.{}".format(ep["guid"], ep["ext"])
        local.write_bytes(b"audio")
        return local

    monkeypatch.setattr(podcasts.navidrome, "ping", ping)
    monkeypatch.setattr(podcasts.navidrome, "play_count", play_count)
    monkeypatch.setattr(podcasts.store, "put", lib.put)
    monkeypatch.setattr(podcasts.store, "delete", lib.delete)
    monkeypatch.setattr(podcasts.tag, "wipe", lambda p: None)
    monkeypatch.setattr(podcasts.tag, "apply", lambda p, **kw: [])
    monkeypatch.setattr(podcasts, "download_episode", download)
    monkeypatch.setattr(podcasts, "fetch_entries", lambda show: feed["entries"])

    state = tmp_path / "podcasts.json"
    podcasts.save({"shows": {"Example Talk Show": _show(min_minutes=10)}}, state)

    class World:
        pass

    w = World()
    w.lib, w.plays, w.nd, w.feed, w.downloads, w.path = lib, plays, nd, feed, downloads, state
    w.sync = lambda: podcasts.sync(log=lambda *a: None, path=state)
    w.show = lambda: podcasts.load(state)["shows"]["Example Talk Show"]
    return w


EP3 = "Example Talk Show/Example Talk Show/2026-10-24 - Episode 3- Long Talk.m4a"


def test_first_sync_publishes_the_newest_episode(world):
    assert world.sync() == 1
    assert world.lib.files == {EP3}
    live = world.show()["live"]
    assert [e["guid"] for e in live] == ["ep-3"]
    assert live[0]["title"] == "2026-10-24 - Episode 3: Long Talk"


def test_unheard_episode_is_kept(world):
    world.sync()
    world.plays["2026-10-24 - Episode 3: Long Talk"] = 0
    assert world.sync() == 0
    assert world.lib.files == {EP3} and world.lib.deleted == []
    assert world.downloads == ["ep-3"]


def test_heard_episode_is_deleted_and_replaced_by_a_newer_one(world):
    world.sync()
    world.plays["2026-10-24 - Episode 3: Long Talk"] = 1
    world.feed["entries"] = [dict(world.feed["entries"][1], guid="ep-4", title="Episode 4",
                                  published="2026-10-31T10:00:00Z")] + world.feed["entries"]
    assert world.sync() == 2
    assert world.lib.deleted == [EP3]
    show = world.show()
    assert [e["guid"] for e in show["live"]] == ["ep-4"]
    assert show["heard"] == ["ep-3"] and show["heard_until"] == "2026-10-24T14:00:00Z"


def test_heard_with_nothing_newer_waits_and_never_refetches(world):
    world.sync()
    world.plays["2026-10-24 - Episode 3: Long Talk"] = 2
    assert world.sync() == 1                      # deleted, nothing to replace it with
    assert world.show()["live"] == []
    assert world.sync() == 0
    assert world.downloads == ["ep-3"]            # neither ep-3 again nor the older ep-2


def test_navidrome_unreachable_deletes_nothing(world):
    world.sync()
    world.plays["2026-10-24 - Episode 3: Long Talk"] = 5
    world.nd["up"] = False
    assert world.sync() == 0
    assert world.lib.deleted == [] and world.lib.files == {EP3}
    assert [e["guid"] for e in world.show()["live"]] == ["ep-3"]


def test_track_not_yet_scanned_is_kept(world):
    world.sync()
    assert world.sync() == 0                      # play_count None: Navidrome has not seen it
    assert world.lib.deleted == []


def test_failed_download_does_not_reach_for_an_older_episode(world, monkeypatch):
    tried = []

    def broken(ep, dest=None, keepalive=None):
        tried.append(ep["guid"])
        raise IOError("connection reset")

    monkeypatch.setattr(podcasts, "download_episode", broken)
    world.show()   # state exists
    podcasts.save({"shows": {"Example Talk Show": _show(min_minutes=10, keep=2)}}, world.path)
    assert world.sync() == 0
    assert tried == ["ep-3"] and world.show()["live"] == []


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
