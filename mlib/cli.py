"""mlib command line - search, stage, review, approve, publish.

Nothing reaches the library without an explicit `approve`, so an agent can fetch
candidates freely and a human still decides what lands.
"""
import argparse
import sys

from . import fetch, navidrome, store, tag
from .config import STAGING, cfg
from .naming import clean, relpath, split_artist_title

AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac"}


def _staged_files():
    STAGING.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in STAGING.iterdir() if p.suffix.lower() in AUDIO_EXTS)


def _fmt_duration(seconds):
    if not seconds:
        return "?:??"
    seconds = int(seconds)
    return "{}:{:02d}".format(seconds // 60, seconds % 60)


def _resolve_selection(selection, count):
    """Turn 'all', '2', or '1,3-4' into a list of zero-based indices."""
    if not selection or selection == "all":
        return list(range(count))
    picked = set()
    for chunk in selection.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            lo, hi = chunk.split("-", 1)
            picked.update(range(int(lo) - 1, int(hi)))
        elif chunk:
            picked.add(int(chunk) - 1)
    return sorted(i for i in picked if 0 <= i < count)


# -- commands ------------------------------------------------------------


def cmd_status(args):
    print("destination : " + cfg.describe())
    print("staging     : {} ({} file(s))".format(STAGING, len(_staged_files())))
    if cfg.nd_url:
        try:
            navidrome.ping()
            print("navidrome   : {} (reachable)".format(cfg.nd_url))
        except Exception as exc:
            print("navidrome   : {} (UNREACHABLE: {})".format(cfg.nd_url, exc))
    return 0


def cmd_search(args):
    results = fetch.search(args.query, limit=args.limit)
    if not results:
        print("no results")
        return 1
    for i, r in enumerate(results, 1):
        print("{:2d}. {}".format(i, r["raw_title"]))
        print("    {}  {}".format(_fmt_duration(r["duration"]), r["channel"] or ""))
        print("    " + r["url"])
    print("\nstage one with:  mlib add <url> --artist ARTIST --album ALBUM")
    return 0


def cmd_add(args):
    meta = fetch.probe(args.url) or {}
    guess_artist, guess_title = split_artist_title(meta.get("raw_title") or "")

    artist = clean(args.artist or meta.get("artist") or guess_artist or "")
    title = clean(args.title or meta.get("title") or guess_title or "")
    album = clean(args.album or meta.get("album") or "")

    if not artist or not title:
        print("could not determine artist/title - pass --artist and --title", file=sys.stderr)
        if meta.get("raw_title"):
            print("source title was: " + meta["raw_title"], file=sys.stderr)
        return 2

    print("downloading: {} - {}".format(artist, title))
    path, _info = fetch.download(args.url)
    fetch.write_sidecar(
        path,
        {
            "artist": artist,
            "album": album or "Singles",
            "title": title,
            "track": args.track,
            "source": args.url,
        },
    )
    target = relpath(artist, album or "Singles", title, args.track)
    print("staged: {}  ->  {}".format(path.name, target))
    return 0


def cmd_staged(args):
    files = _staged_files()
    if not files:
        print("staging is empty")
        return 0
    for i, path in enumerate(files, 1):
        meta = fetch.read_sidecar(path)
        target = relpath(
            meta.get("artist"),
            meta.get("album"),
            meta.get("title"),
            meta.get("track"),
            path.suffix,
        )
        size = path.stat().st_size / 1000000.0
        print("{:2d}. {}   ({:.1f} MB)".format(i, target, size))
        if meta.get("source"):
            print("    from " + meta["source"])
    print("\napprove with:  mlib approve all   |   reject with:  mlib reject 2")
    return 0


def cmd_approve(args):
    files = _staged_files()
    indices = _resolve_selection(args.selection, len(files))
    if not indices:
        print("nothing to approve")
        return 0

    published = 0
    for i in indices:
        path = files[i]
        meta = fetch.read_sidecar(path)
        artist = meta.get("artist") or "Unknown Artist"
        album = meta.get("album") or "Singles"
        title = meta.get("title") or path.stem
        track = meta.get("track")

        tag.apply(path, artist=artist, album=album, title=title, track=track)
        target = relpath(artist, album, title, track, path.suffix)

        if store.exists(target) and not args.force:
            print("skip (already in library): " + target)
            continue

        store.put(path, target)
        fetch.sidecar_path(path).unlink(missing_ok=True)
        print("published: " + target)
        published += 1

    if published and cfg.nd_url and cfg.nd_pass:
        try:
            navidrome.scan()
            print("navidrome rescan triggered")
        except Exception as exc:
            print("(rescan skipped: {})".format(exc))
    return 0


def cmd_reject(args):
    files = _staged_files()
    indices = _resolve_selection(args.selection, len(files))
    for i in indices:
        path = files[i]
        fetch.sidecar_path(path).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        print("discarded: " + path.name)
    return 0


def cmd_ls(args):
    if args.local:
        for rel in store.listing():
            print(rel)
        return 0
    for album in navidrome.albums():
        print(
            "{} - {}  ({} tracks)".format(
                album.get("artist"), album.get("name"), album.get("songCount")
            )
        )
        if args.tracks:
            for song in navidrome.tracks(album["id"]):
                print("    {:>3}  {}".format(song.get("track") or "-", song.get("title")))
    return 0


def cmd_rm(args):
    if not args.yes:
        print("about to delete: " + args.path)
        if input("type 'yes' to confirm: ").strip().lower() != "yes":
            print("aborted")
            return 1
    if store.delete(args.path):
        print("deleted: " + args.path)
        try:
            navidrome.scan()
        except Exception:
            pass
        return 0
    print("not found: " + args.path)
    return 1


def cmd_scan(args):
    navidrome.scan()
    print("rescan triggered")
    return 0


# -- wiring --------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(prog="mlib", description="self-hosted music library manager")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show configuration and connectivity").set_defaults(
        func=cmd_status
    )

    s = sub.add_parser("search", help="search sources for candidates (no download)")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=8)
    s.set_defaults(func=cmd_search)

    a = sub.add_parser("add", help="download a URL into staging")
    a.add_argument("url")
    a.add_argument("--artist")
    a.add_argument("--album")
    a.add_argument("--title")
    a.add_argument("--track", type=int)
    a.set_defaults(func=cmd_add)

    sub.add_parser("staged", help="list what is waiting for approval").set_defaults(
        func=cmd_staged
    )

    ap = sub.add_parser("approve", help="tag and publish staged files")
    ap.add_argument("selection", nargs="?", default="all", help="'all', '2', or '1,3-4'")
    ap.add_argument("--force", action="store_true", help="overwrite an existing library file")
    ap.set_defaults(func=cmd_approve)

    rj = sub.add_parser("reject", help="discard staged files")
    rj.add_argument("selection", nargs="?", default="all")
    rj.set_defaults(func=cmd_reject)

    ls = sub.add_parser("ls", help="list the live library")
    ls.add_argument("--tracks", action="store_true", help="also list tracks per album")
    ls.add_argument("--local", action="store_true", help="list files on disk (direct mode)")
    ls.set_defaults(func=cmd_ls)

    rm = sub.add_parser("rm", help="delete a track from the library")
    rm.add_argument("path", help="library-relative path, e.g. Artist/Album/Song.mp3")
    rm.add_argument("-y", "--yes", action="store_true")
    rm.set_defaults(func=cmd_rm)

    sub.add_parser("scan", help="trigger a Navidrome rescan").set_defaults(func=cmd_scan)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
