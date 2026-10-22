"""mlib command line - search, stage, review, approve, publish.

Nothing reaches the library without an explicit `approve`, so an agent can fetch
candidates freely and a human still decides what lands.
"""
import argparse
import sys

from . import agent, batch, catalog, check, fetch, navidrome, providers, store, tag
from .config import STAGING, cfg
from .naming import VARIOUS, clean, relpath, split_artist_title

AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac"}


def _staged_files():
    STAGING.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in STAGING.iterdir() if p.suffix.lower() in AUDIO_EXTS)


def _fmt_duration(seconds):
    if not seconds:
        return "?:??"
    seconds = int(seconds)
    return "{}:{:02d}".format(seconds // 60, seconds % 60)


def _target(meta, ext):
    """Library path for a staged track, from its sidecar."""
    return relpath(meta.get("artist"), meta.get("album"), meta.get("title"), meta.get("track"),
                   ext, collection=meta.get("collection", False))


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


def _labels(r):
    """'1936, instr, film' - what a provider knows beyond the title."""
    bits = [str(r["year"])] if r.get("year") else []
    if r.get("vocal") is not None:
        bits.append("vocal" if r["vocal"] else "instr")
    return ", ".join(bits + list(r.get("flags") or []))


def cmd_search(args):
    results = providers.search(args.query, provider=args.provider, limit=args.limit)
    if not results:
        print("no results")
        return 1
    for i, r in enumerate(results, 1):
        print("{:2d}. {}".format(i, r["raw_title"]))
        labels = _labels(r)
        print("    {}  {}{}".format(_fmt_duration(r["duration"]),
                                    r.get("channel") or r.get("performer") or "",
                                    "  [{}]".format(labels) if labels else ""))
        print("    " + r["url"])
    print("\nstage one with:  mlib add <url> --artist ARTIST --album ALBUM")
    return 0


def cmd_add(args):
    meta = providers.probe(args.url) or {}
    guess_artist, guess_title = split_artist_title(meta.get("raw_title") or "")

    artist = clean(args.artist or meta.get("artist") or guess_artist or "")
    title = clean(args.title or meta.get("title") or guess_title or "")
    album = clean(args.collection or args.album or meta.get("album") or "")

    if not artist or not title:
        print("could not determine artist/title - pass --artist and --title", file=sys.stderr)
        if meta.get("raw_title"):
            print("source title was: " + meta["raw_title"], file=sys.stderr)
        return 2

    bad = _catalog_try(lambda cat: catalog.blocked(cat, args.url), save=False)
    if bad and not args.anyway:
        print("refusing: this source is {} in the catalog ({})".format(
            bad["status"], bad.get("reason") or "no reason given"), file=sys.stderr)
        print("pass --anyway to stage it regardless", file=sys.stderr)
        return 2

    print("downloading: {} - {}".format(artist, title))
    path, _info = providers.download(args.url)
    meta = {
        "artist": artist,
        "album": album or "Singles",
        "title": title,
        "track": args.track,
        "source": providers.canonical_url(args.url),
        "collection": bool(args.collection),
    }
    meta.update(_catalog_try(lambda cat: catalog.stage(cat, meta, song=args.song,
                                                       vname=args.version)) or {})
    fetch.write_sidecar(path, meta)
    target = relpath(artist, album or "Singles", title, args.track, collection=bool(args.collection))
    print("staged: {}  ->  {}".format(path.name, target))
    return 0


def cmd_staged(args):
    files = _staged_files()
    if not files:
        print("staging is empty")
        return 0
    for i, path in enumerate(files, 1):
        meta = fetch.read_sidecar(path)
        target = _target(meta, path.suffix)
        size = path.stat().st_size / 1000000.0
        verdict = check.cached_verdict(meta)
        flag = {None: "  [unchecked]", "ok": ""}.get(verdict, "  [HELD: {}]".format(verdict))
        print("{:2d}. {}   ({:.1f} MB){}".format(i, target, size, flag))
        if verdict not in (None, "ok"):
            print("    " + meta["check"]["detail"])
        if meta.get("source"):
            print("    from " + meta["source"])
    print("\ncheck with:    mlib check   (approve runs it too)")
    print("approve with:  mlib approve all   |   reject with:  mlib reject 2")
    return 0


def _run_check(files, library=True):
    return check.check_files(files, fetch.read_sidecar, fetch.write_sidecar, library=library)


def cmd_check(args):
    files = _staged_files()
    picked = [files[i] for i in _resolve_selection(args.selection, len(files))]
    if not picked:
        print("staging is empty")
        return 0
    print("listening to {} staged file(s)".format(len(picked)))
    results = _run_check(picked, library=not args.staged_only)
    _catalog_try(lambda cat: [catalog.note_check(cat, fetch.read_sidecar(r[0])) for r in results])
    held = [r for r in results if r[1] != "ok"]
    print("\n{} ok, {} held".format(len(results) - len(held), len(held)))
    if held:
        print("held files are skipped by `approve`. listen and `mlib reject N`, or")
        print("publish one anyway with `mlib approve N --anyway`.")
    return 0


def cmd_approve(args):
    files = _staged_files()
    indices = _resolve_selection(args.selection, len(files))
    if not indices:
        print("nothing to approve")
        return 0

    unchecked = [files[i] for i in indices if not check.cached_verdict(fetch.read_sidecar(files[i]))]
    if unchecked and not args.anyway:
        print("checking {} file(s) first".format(len(unchecked)))
        _run_check(unchecked)

    published = 0
    for i in indices:
        path = files[i]
        meta = fetch.read_sidecar(path)
        verdict = check.cached_verdict(meta)
        if verdict not in (None, "ok") and not args.anyway:
            print("held ({}): {}  - {}".format(verdict, path.name, meta["check"]["detail"]))
            continue
        artist = meta.get("artist") or "Unknown Artist"
        album = meta.get("album") or "Singles"
        title = meta.get("title") or path.stem
        track = meta.get("track")
        collection = meta.get("collection", False)

        if providers.detect(meta.get("source")) != "youtube":
            tag.wipe(path)      # sovmusic tags are cp1251 mojibake; none may survive
        tag.apply(path, artist=artist, album=album, title=title, track=track,
                  albumartist=VARIOUS if collection else None, compilation=collection)
        target = _target(meta, path.suffix)

        replaces = meta.get("replaces") or {}
        if store.exists(target) and not args.force and not replaces:
            print("skip (already in library): " + target)
            continue

        store.put(path, target)
        fetch.sidecar_path(path).unlink(missing_ok=True)
        print("published: " + target)
        old_path = replaces.get("library_path")
        if old_path and old_path != target:
            store.delete(old_path)
            print("  replaced: " + old_path)
        _catalog_try(lambda cat: catalog.published(cat, meta, target))
        published += 1

    if published:
        _rescan()
    return 0


def _rescan():
    """Tell Navidrome to pick up changes, or say plainly that it did not happen."""
    if not (cfg.nd_url and cfg.nd_pass):
        print("navidrome NOT rescanned: set MLIB_ND_URL / MLIB_ND_USER / MLIB_ND_PASS in .env,")
        print("  or press scan in the Navidrome web UI. New files stay invisible until then.")
        return
    try:
        navidrome.scan()
        print("navidrome rescan triggered")
    except Exception as exc:
        print("navidrome NOT rescanned ({}) - press scan in the web UI".format(exc))


def cmd_reject(args):
    files = _staged_files()
    indices = _resolve_selection(args.selection, len(files))
    for i in indices:
        path = files[i]
        meta = fetch.read_sidecar(path)
        reason = args.reason or catalog.check_reason(meta) or "rejected at review"
        if meta.get("source"):
            _catalog_try(lambda cat: catalog.rejected(cat, meta, reason))
        fetch.sidecar_path(path).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        print("discarded: {}  ({})".format(path.name, reason))
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
        _catalog_try(lambda cat: catalog.removed(cat, args.path, args.reason))
        _rescan()
        return 0
    print("not found: " + args.path)
    return 1


def cmd_scan(args):
    navidrome.scan()
    print("rescan triggered")
    return 0


def cmd_auto(args):
    """Search, let the model pick real songs, stage them. Never publishes."""
    print("searching: {}".format(args.request))
    candidates = fetch.search(args.request, limit=args.pool)
    if not candidates:
        print("no results")
        return 1

    est, kept = agent.estimate_tokens(args.request, candidates, limit=args.limit)
    print("{} results -> {} after filtering (~{} prompt tokens)".format(
        len(candidates), kept, est))
    if kept == 0:
        print("nothing survived the filter; try a more specific request")
        return 1

    if args.dry_run:
        for c in agent.prefilter(candidates):
            print("  {}  {}".format(_fmt_duration(c["duration"]), c["raw_title"]))
        print("\n(dry run - no model call, nothing downloaded)")
        return 0

    picks = agent.choose(
        args.request, candidates, limit=args.limit, backend=args.backend
    )
    if not picks:
        print("model picked nothing")
        return 1

    staged = 0
    for candidate, meta in picks:
        artist = clean(meta["artist"])
        title = clean(meta["title"])
        album = clean(meta["album"]) or "Singles"
        if not artist or not title:
            print("skip (incomplete metadata): {}".format(candidate["raw_title"]))
            continue

        target = relpath(artist, album, title)
        if store.exists(target):
            print("skip (already in library): {}".format(target))
            continue

        print("downloading: {} - {}".format(artist, title))
        try:
            path, _info = fetch.download(candidate["url"])
        except Exception as exc:
            print("  failed: {}".format(exc))
            continue
        fetch.write_sidecar(
            path,
            {
                "artist": artist,
                "album": album,
                "title": title,
                "track": None,
                "source": candidate["url"],
            },
        )
        staged += 1

    print("\nstaged {} track(s). review with `mlib staged`, then `mlib approve`".format(staged))
    return 0


def cmd_batch(args):
    """Fetch a known list of songs. Deterministic - no model, no tokens."""
    from pathlib import Path as _Path

    text = _Path(args.file).read_text(encoding="utf-8")
    entries, bad = batch.parse_list(text)
    if bad:
        print("skipping {} unparseable line(s); expected 'Artist - Title':".format(len(bad)))
        for line in bad[:5]:
            print("  " + line)
    if not entries:
        print("nothing to do")
        return 1
    if args.album or args.collection:
        for e in entries:
            e["album"] = args.collection or args.album

    print("resolving {} track(s), {} at a time (no tokens used)".format(len(entries), args.jobs))
    done = [0]

    def progress(entry, confidence, candidate):
        done[0] += 1
        mark = "ok " if confidence >= args.threshold else "?? "
        print("  [{}/{}] {}{} - {}  ({:.2f})".format(
            done[0], len(entries), mark, entry["artist"], entry["title"], confidence))

    results = batch.resolve_all(
        entries, pool=args.pool, jobs=args.jobs, progress=progress, keep=args.keep
    )

    confident = [(e, c, cand) for e, c, cand in results if cand and c >= args.threshold]
    unsure = [(e, c, cand) for e, c, cand in results if not cand or c < args.threshold]

    # Bare titles carry no artist. One batched call fills them all in; without it
    # we fall back to whatever the source metadata suggests.
    needs = [(i, e, cand) for i, (e, c, cand) in enumerate(confident) if not e.get("artist")]
    if needs and not args.no_assist:
        rows = [
            {"i": i, "asked": e["raw"], "matched": cand["raw_title"], "channel": cand.get("channel")}
            for i, e, cand in needs
        ]
        est = agent.estimate_attribute_tokens(rows)
        print("\n{} title(s) have no artist - resolving in ONE call (~{} prompt tokens)".format(
            len(rows), est))
        try:
            filled = agent.attribute(rows, backend=args.backend)
            for i, entry, _cand in needs:
                got = filled.get(i)
                if not got or not got.get("artist"):
                    continue
                entry["artist"] = got["artist"]
                if entry["album"] == "Singles" and got.get("album"):
                    entry["album"] = got["album"]
                if got.get("title"):
                    entry["title"] = got["title"]
        except Exception as exc:
            print("attribution failed ({}); falling back to source metadata".format(exc))

    for _i, entry, cand in needs:
        if not entry.get("artist"):
            guessed, _t = split_artist_title(cand["raw_title"])
            entry["artist"] = clean(guessed or cand.get("channel") or "Unknown Artist")

    if args.dry_run:
        print("\nwould stage {} track(s):".format(len(confident)))
        for entry, confidence, candidate in confident:
            print("  {}  <- {}".format(
                relpath(entry["artist"], entry["album"], entry["title"],
                        collection=bool(args.collection)),
                candidate["raw_title"]))
        _report_unsure(unsure)
        print("\n(dry run - nothing downloaded)")
        return 0

    staged = 0
    for entry, confidence, candidate in confident:
        target = relpath(entry["artist"], entry["album"], entry["title"],
                         collection=bool(args.collection))
        if store.exists(target):
            print("skip (already in library): " + target)
            continue
        # Sources go missing or turn out to be age-gated, so fall back through
        # the runners-up for the same song rather than dropping it.
        sources = [candidate] + (entry.get("_alternates") or [])
        path = used = None
        last_error = None
        for attempt, source in enumerate(sources):
            try:
                path, _info = fetch.download(source["url"])
                used = source
                if attempt:
                    print("  used alternate source for {} - {}".format(
                        entry["artist"], entry["title"]))
                break
            except Exception as exc:
                last_error = exc

        if not path:
            print("download failed for {} - {} (tried {} source(s)): {}".format(
                entry["artist"], entry["title"], len(sources), last_error))
            continue

        fetch.write_sidecar(path, {
            "artist": entry["artist"],
            "album": entry["album"],
            "title": entry["title"],
            "track": None,
            "source": used["url"],
            "collection": bool(args.collection),
        })
        staged += 1

    print("\nstaged {} track(s). review with `mlib staged`, then `mlib approve`".format(staged))
    _report_unsure(unsure)
    return 0


def _report_unsure(unsure):
    if not unsure:
        return
    print("\n{} needed a closer look - not staged:".format(len(unsure)))
    for entry, confidence, candidate in unsure:
        detail = candidate["raw_title"] if candidate else "no usable result"
        print("  {} - {}  ({:.2f})  best was: {}".format(
            entry["artist"], entry["title"], confidence, detail))
    print("stage one by hand with:  mlib add <url> --artist ... --title ...")


# -- catalog -------------------------------------------------------------


def _catalog_try(change, save=True):
    """Apply a change to the catalog. A catalog problem never blocks a file operation."""
    try:
        if not save:
            return change(catalog.load())
        with catalog.edit() as cat:
            return change(cat)
    except Exception as exc:
        print("catalog NOT updated: {}".format(exc), file=sys.stderr)
        return None


def _one_song(cat, ref):
    hits = catalog.find(cat, ref)
    if len(hits) == 1:
        return hits[0]
    if hits:
        print("'{}' matches {} songs:".format(ref, len(hits)))
        for sid in hits:
            print("  " + sid)
    else:
        print("no song matches '{}'".format(ref))
    return None


def _cell(song, vname):
    v = song["versions"].get(vname)
    if not v:
        return "-"
    who = catalog.performer(v)
    return "{:8} {}".format(v["status"], (who or "")[:34])


def cmd_songs(args):
    cat = catalog.load()
    wanted = catalog.VERSIONS if args.missing == "any" else [args.missing]
    rows = []
    for sid, song in sorted(cat["songs"].items()):
        if args.theme and song["theme"] != args.theme:
            continue
        if args.collection and args.collection not in (
                v["collection"] for v in song["versions"].values()):
            continue
        if args.missing and not any(catalog.missing(song, w) for w in wanted):
            continue
        rows.append((sid, song))
    if args.find:
        for sid, song in rows:
            for vname in wanted if args.missing else catalog.VERSIONS:
                if catalog.missing(song, vname):
                    _find_sources(sid, vname, args.find, args.limit)
        return 0
    print("{:42} {:44} {}".format("song", "vocal", "instrumental"))
    for sid, song in rows:
        print("{:42} {:44} {}".format(sid[:42], _cell(song, "vocal"), _cell(song, "instrumental")))
    print("\n{} song(s). details: mlib song <id-or-title>".format(len(rows)))
    return 0


def cmd_song(args):
    cat = catalog.load()
    sid = _one_song(cat, args.ref)
    if not sid:
        return 1
    song = cat["songs"][sid]
    print("{}  ({})".format(song["title"], sid))
    if song.get("aliases"):
        print("aliases: " + ", ".join(song["aliases"]))
    if song.get("notes"):
        print("notes: " + song["notes"])
    for vname, v in sorted(song["versions"].items()):
        print("\n{}: {}  [{}]".format(vname, v["status"], v["collection"] or v["album"] or ""))
        if v["library_path"]:
            print("  file: " + v["library_path"])
        for s in v["sources"]:
            # * the current source, + another recording that is live as well
            mark = "*" if s["id"] == v["current"] else "+" if s["status"] == "live" else " "
            print(" {} {:9} {}  {}".format(mark, s["status"], s.get("performer") or "?",
                                           s.get("url") or "(origin unknown)"))
            if s.get("reason"):
                print("             " + s["reason"])
            if s["status"] == "live" and s.get("library_path") != v["library_path"]:
                print("             file: " + s["library_path"])
    return 0


def cmd_source_add(args):
    with catalog.edit() as cat:
        return _source_add(cat, args)


def _source_add(cat, args):
    hits = catalog.find(cat, args.song)
    if len(hits) > 1:
        print("'{}' matches several songs: {}".format(args.song, ", ".join(hits)), file=sys.stderr)
        return 2
    if not hits and not args.theme:
        print("no song '{}' yet - pass --theme to create it".format(args.song), file=sys.stderr)
        return 2
    bad = catalog.blocked(cat, args.url)
    if bad and not args.anyway:
        print("refusing: source is {} ({})".format(bad["status"], bad.get("reason")),
              file=sys.stderr)
        return 2
    theme = args.theme or cat["songs"][hits[0]]["theme"]
    collection = args.collection or (
        None if theme == catalog.OTHER else "{} {}".format(theme, args.version))
    sid, vname, s = catalog.record(
        cat, args.url, args.title or (cat["songs"][hits[0]]["title"] if hits else args.song),
        args.performer, collection=collection, theme=theme, vname=args.version,
        song=hits[0] if hits else args.song)
    print("{} {}: {} {} ({})".format(sid, vname, s["status"], s["id"], s.get("performer")))
    return 0


def cmd_replace(args):
    """Stage another source for a live version; `approve` swaps it in for the current file."""
    return _stage_candidate(args, replacing=True)


def cmd_fill(args):
    """Stage the best candidate for a version that is not in the library yet."""
    return _stage_candidate(args, replacing=False)


def _stage_candidate(args, replacing):
    with catalog.edit() as cat:
        sid = _one_song(cat, args.song)
        if not sid:
            return 1
        song = cat["songs"][sid]
        v = song["versions"].get(args.version)
        if v is None and not args.url:
            print("{} has no {} version - `mlib source find` or `source add` first".format(
                sid, args.version))
            return 1
        if v and not replacing and v["status"] in ("live", "staged"):
            print("{} {} is already {} - use `mlib replace` for a better source".format(
                sid, args.version, v["status"]))
            return 1
        current = catalog.source(v, v["current"]) if v else None
        if args.url:
            bad = catalog.blocked(cat, args.url)
            if bad and not args.anyway:
                print("refusing: source is {} ({})".format(bad["status"], bad.get("reason")))
                return 2
            hit = catalog.locate(cat, args.url)
            theme = song["theme"]
            collection = (v or {}).get("collection") or (
                None if theme == catalog.OTHER else "{} {}".format(theme, args.version))
            new = hit[2] if hit else catalog.record(
                cat, args.url, (current or {}).get("title") or song["title"],
                args.performer or (current or {}).get("performer"), collection=collection,
                album=(v or {}).get("album"), song=sid, vname=args.version)[2]
            v = song["versions"][args.version]
        else:
            provider = providers.name(args.provider) if args.provider else None
            new = catalog.next_candidate(v, provider=provider)
            if not new:
                print("no {}candidate for {} {} - `mlib source find`, or pass --url".format(
                    provider + " " if provider else "", sid, args.version))
                return 1
        if args.performer:
            new["performer"] = args.performer
        if not new.get("performer"):
            print("no performer known for {} - pass --performer".format(new["url"]))
            return 2
        meta = {
            "artist": new["performer"],
            "album": v["collection"] or v["album"] or "Singles",
            "title": (current or new).get("title") or song["title"],
            "track": None,
            "source": new["url"],
            "collection": bool(v["collection"]),
            "song": sid,
            "version": args.version,
            "source_id": new["id"],
        }
        if replacing:
            meta["replaces"] = {
                "source_id": current["id"] if current else None,
                "library_path": v["library_path"],
                "reason": args.reason,
            }

    print("downloading: {} - {}  ({})".format(meta["artist"], song["title"], meta["source"]))
    path, _info = providers.download(meta["source"])
    fetch.write_sidecar(path, meta)
    with catalog.edit() as cat:
        _sid, vname, s = catalog.locate(cat, sid=meta["source_id"])
        s["status"], s["reason"] = "staged", None
        catalog.settle(cat["songs"][sid]["versions"][vname])
    old = (meta.get("replaces") or {}).get("library_path")
    print("staged: {}{}".format(path.name, "  - `mlib approve` swaps it in for " + old if old
                                else "  - review with `mlib check` and `mlib staged`"))
    return 0


def cmd_source_find(args):
    return 0 if _find_sources(args.song, args.version, args.provider, args.limit,
                              theme=args.theme) is not None else 1


def _find_sources(ref, vname, provider, limit, theme=None):
    """Search one provider for a song version and record the best hits as
    candidates. Downloads nothing. Returns the recorded results, or None."""
    provider = providers.name(provider)
    cat = catalog.load()
    hits = catalog.find_exact(cat, ref)
    if len(hits) > 1:
        print("'{}' is several songs: {}".format(ref, ", ".join(hits)))
        return None
    if hits:
        sid = hits[0]
        song = cat["songs"][sid]
        theme = song["theme"]
    else:
        # sovmusic is a Soviet archive; anything else needs to be told the theme
        theme = theme or ("sov" if provider == "sovmusic" else None)
        if not theme:
            print("no song '{}' yet - pass --theme to create it".format(ref))
            return None
        sid, song = None, {"title": clean(ref), "aliases": []}

    print("searching {} for {} ({})".format(provider, song["title"], vname))
    results = providers.search(song["title"], provider=provider, limit=40)
    kept, dropped = catalog.pick_sources(song, vname, results, cat)
    kept = kept[:limit]
    collection = None if theme == catalog.OTHER else "{} {}".format(theme, vname)
    with catalog.edit() as cat:
        for r in kept:
            sid, _v, src = catalog.record(
                cat, r["url"], song["title"], r.get("performer"), status="candidate",
                collection=collection, theme=theme, vname=vname, song=sid or song["title"],
                merge=True, extra={"year": r.get("year"), "flags": r.get("flags") or None})
            print("  {:9} {}  {}  {}".format(src["status"], r.get("year") or "----",
                                             r.get("performer") or "?", src["id"]))
    if not kept:
        print("  nothing usable")
    reasons = {}
    for _r, why in dropped:
        reasons[why] = reasons.get(why, 0) + 1
    if reasons:
        print("  skipped: " + ", ".join("{} {}".format(n, why) for why, n in sorted(reasons.items())))
    if kept:
        print("stage the first with:  mlib fill {} {}".format(sid, vname))
    return kept


def _read_history(path):
    import csv

    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh, delimiter="\t"))
    keys = ("artist", "title", "url", "status", "info", "collection")
    return [dict(zip(keys, r)) for r in rows[1:] if len(r) >= 4]


def _read_picks(pattern):
    import glob

    out = []
    for name in sorted(glob.glob(pattern)):
        with open(name, encoding="utf-8") as fh:
            for line in fh:
                bits = [b.strip() for b in line.split("|")]
                if len(bits) == 5 and bits[0] == "ADD":
                    out.append(dict(zip(("verb", "url", "artist", "title", "collection"), bits)))
    return out


def _read_sidecars():
    import json

    out = []
    for p in sorted(STAGING.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            print("skipping unreadable sidecar (still being written?): " + p.name)
    return out


def _tally(items):
    counts = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return ", ".join("{} {}".format(n, k) for k, n in sorted(counts.items()))


def cmd_catalog_import(args):
    history = _read_history(args.history) if args.history else []
    picks = _read_picks(args.picks) if args.picks else []
    listing = store.listing()
    sidecars = _read_sidecars()
    print("library {} file(s), history {} row(s), staging {} sidecar(s), picks {} line(s)".format(
        len(listing), len(history), len(sidecars), len(picks)))

    with catalog.edit() as cat:
        stats = catalog.import_all(cat, listing, history, sidecars, picks, theme=args.theme)

    versions = [v for s in cat["songs"].values() for v in s["versions"].values()]
    print("songs:    {}".format(len(cat["songs"])))
    print("versions: " + _tally(versions))
    print("sources:  " + _tally(s for v in versions for s in v["sources"]))
    if stats["unknown_origin"]:
        print("{} live file(s) with no known source URL".format(stats["unknown_origin"]))
    for line in stats["gone"]:
        print("in history as live, but not in the library: " + line)
    for line in stats["merged"]:
        print("merged by alias: " + line)
    near = catalog.near_matches(cat)
    if near:
        print("\nNOT merged - add an alias in catalog/songs.json if these are one song:")
        for a, b in near:
            print("  {}  ~  {}".format(a, b))
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
    s.add_argument("--provider", choices=("youtube", "sovmusic", "archive"), default="youtube",
                   help="sovmusic: Soviet records, labelled vocal/instr/film; archive: "
                        "archive.org, English or transliterated queries only")
    s.set_defaults(func=cmd_search)

    a = sub.add_parser("add", help="download a URL (YouTube, sovmusic.ru, archive.org) "
                                   "into staging")
    a.add_argument("url")
    a.add_argument("--artist")
    a.add_argument("--album")
    a.add_argument("--title")
    a.add_argument("--track", type=int)
    a.add_argument("--collection", metavar="ALBUM",
                   help="put it in a many-artist album, e.g. --collection sov")
    a.add_argument("--song", help="catalog song id or title (default: from title + collection)")
    a.add_argument("--version", choices=catalog.VERSIONS,
                   help="vocal or instrumental (default: from the collection name)")
    a.add_argument("--anyway", action="store_true",
                   help="stage it even if the catalog says this source was rejected")
    a.set_defaults(func=cmd_add)

    au = sub.add_parser("auto", help="describe what you want; the model picks songs")
    au.add_argument("request")
    au.add_argument("--limit", type=int, default=5, help="max tracks to stage")
    au.add_argument("--pool", type=int, default=25, help="how many results to search")
    au.add_argument("--backend", help="LLM CLI to use (default: codex, then claude)")
    au.add_argument("--dry-run", action="store_true", help="filter only, no model call")
    au.set_defaults(func=cmd_auto)

    ba = sub.add_parser("batch", help="fetch a known list of songs (no model, no tokens)")
    ba.add_argument("file", help="text file, one 'Artist - Title' per line, '# comment' ok")
    ba.add_argument("--album", help="album for every entry (default: per-line, else Singles)")
    ba.add_argument("--collection", metavar="ALBUM",
                    help="put every entry in one many-artist album, e.g. --collection sov")
    ba.add_argument("--threshold", type=float, default=0.62, help="match confidence 0-1")
    ba.add_argument("--pool", type=int, default=6, help="results to consider per song")
    ba.add_argument("--keep", type=int, default=4,
                    help="fallback sources to try if a download fails")
    ba.add_argument("--jobs", type=int, default=4, help="parallel searches")
    ba.add_argument("--dry-run", action="store_true", help="show matches, download nothing")
    ba.add_argument("--no-assist", action="store_true",
                    help="do not call a model to fill in missing artists")
    ba.add_argument("--backend", help="LLM CLI for artist lookup (default: codex, then claude)")
    ba.set_defaults(func=cmd_batch)

    sub.add_parser("staged", help="list what is waiting for approval").set_defaults(
        func=cmd_staged
    )

    ap = sub.add_parser("approve", help="tag and publish staged files")
    ap.add_argument("selection", nargs="?", default="all", help="'all', '2', or '1,3-4'")
    ap.add_argument("--force", action="store_true", help="overwrite an existing library file")
    ap.add_argument("--anyway", action="store_true",
                    help="publish even if `check` held it (skips checking)")
    ap.set_defaults(func=cmd_approve)

    ck = sub.add_parser("check", help="listen for non-songs and duplicates before approving")
    ck.add_argument("selection", nargs="?", default="all")
    ck.add_argument("--staged-only", action="store_true",
                    help="compare staged files with each other, not the whole library")
    ck.set_defaults(func=cmd_check)

    rj = sub.add_parser("reject", help="discard staged files")
    rj.add_argument("selection", nargs="?", default="all")
    rj.add_argument("--reason", help="why, kept in the catalog (default: the check verdict)")
    rj.set_defaults(func=cmd_reject)

    ls = sub.add_parser("ls", help="list the live library")
    ls.add_argument("--tracks", action="store_true", help="also list tracks per album")
    ls.add_argument("--local", action="store_true", help="list files on disk (direct mode)")
    ls.set_defaults(func=cmd_ls)

    rm = sub.add_parser("rm", help="delete a track from the library")
    rm.add_argument("path", help="library-relative path, e.g. Artist/Album/Song.mp3")
    rm.add_argument("-y", "--yes", action="store_true")
    rm.add_argument("--reason", help="why, kept in the catalog")
    rm.set_defaults(func=cmd_rm)

    so = sub.add_parser("songs", help="list catalog songs and which versions are live")
    so.add_argument("--theme")
    so.add_argument("--collection")
    so.add_argument("--find", choices=("youtube", "sovmusic", "archive"),
                    help="run `source find` on every missing version, one at a time")
    so.add_argument("--limit", type=int, default=3, help="candidates per version with --find")
    so.add_argument("--missing", choices=catalog.VERSIONS + ("any",),
                    help="only songs lacking this version (not live, staged or none-exists)")
    so.set_defaults(func=cmd_songs)

    sg = sub.add_parser("song", help="one song's versions and every source tried")
    sg.add_argument("ref", help="song id (sov/катюша) or title")
    sg.set_defaults(func=cmd_song)

    src = sub.add_parser("source", help="record sources in the catalog")
    src_sub = src.add_subparsers(dest="source_command", required=True)
    sa = src_sub.add_parser("add", help="record a candidate source for a song version")
    sa.add_argument("song", help="song id or title (a new title creates the song)")
    sa.add_argument("version", choices=catalog.VERSIONS)
    sa.add_argument("url")
    sa.add_argument("--performer", required=True)
    sa.add_argument("--theme", help="needed when the song is new, e.g. sov")
    sa.add_argument("--collection", help="default: '<theme> <version>'")
    sa.add_argument("--title", help="track title, if not the song title")
    sa.add_argument("--anyway", action="store_true", help="record it even if rejected before")
    sa.set_defaults(func=cmd_source_add)
    sf = src_sub.add_parser("find", help="search a provider, record the best hits as candidates")
    sf.add_argument("song", help="song id or exact title (a new title creates the song)")
    sf.add_argument("version", choices=catalog.VERSIONS)
    sf.add_argument("--provider", choices=("youtube", "sovmusic", "archive"), default="sovmusic",
                    help="default sovmusic; archive needs an English/transliterated title")
    sf.add_argument("--limit", type=int, default=3, help="how many candidates to record")
    sf.add_argument("--theme", help="for a new song (default: sov with sovmusic)")
    sf.set_defaults(func=cmd_source_find)

    fi = sub.add_parser("fill", help="stage the best candidate for a version not yet live")
    fi.add_argument("song")
    fi.add_argument("version", choices=catalog.VERSIONS)
    fi.add_argument("--url", help="this source instead of the next candidate")
    fi.add_argument("--provider", choices=("youtube", "sovmusic", "archive"),
                    help="only consider candidates from this provider")
    fi.add_argument("--performer", help="performer of the source")
    fi.add_argument("--anyway", action="store_true", help="use it even if rejected before")
    fi.set_defaults(func=cmd_fill)

    rp = sub.add_parser("replace", help="stage a better source; approve swaps it in")
    rp.add_argument("song")
    rp.add_argument("version", choices=catalog.VERSIONS)
    how = rp.add_mutually_exclusive_group(required=True)
    how.add_argument("--url", help="this source")
    how.add_argument("--next", action="store_true", help="the next candidate in the catalog")
    rp.add_argument("--reason", required=True, help="what is wrong with the current one")
    rp.add_argument("--provider", choices=("youtube", "sovmusic", "archive"),
                    help="with --next: only candidates from this provider")
    rp.add_argument("--performer", help="performer of the new source")
    rp.add_argument("--anyway", action="store_true", help="use it even if rejected before")
    rp.set_defaults(func=cmd_replace)

    ca = sub.add_parser("catalog", help="maintain catalog/songs.json")
    ca_sub = ca.add_subparsers(dest="catalog_command", required=True)
    ci = ca_sub.add_parser("import", help="build or refresh the catalog from what exists")
    ci.add_argument("--history", help="TSV: artist, title, url, status, path-or-reason")
    ci.add_argument("--picks", help="glob of pick files, lines 'ADD | url | artist | title | coll'")
    ci.add_argument("--theme", default="sov",
                    help="theme for history rows that name no collection (default: sov)")
    ci.set_defaults(func=cmd_catalog_import)

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
