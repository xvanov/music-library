# music-library

Tooling for a self-hosted music setup: **Navidrome** on a home Linux box, played
on iPhone through **Amperfy**, with a CLI (`mlib`) for finding, tagging and
publishing tracks.

## What the pieces are

| Piece | Where | Job |
|---|---|---|
| `navidrome` | home server, port 4533 | indexes the library, serves the Subsonic API |
| `uploader` (dufs) | home server, port 4534 | WebDAV + web UI for getting files in |
| `tagger` | home server, container | fills missing tags from the folder layout |
| `mlib` | this repo | search, download, tag, publish, prune |
| Amperfy | iPhone | plays it, including with the screen off |

Everything binds to the **tailnet address only**. Nothing is on the public
internet. Cost is zero — no paid hosting, no subscriptions.

## The one rule that explains everything

**Navidrome reads ID3 tags. It ignores folder names.**

An untagged file shows up as `[Unknown Artist] / [Unknown Album]` no matter
where you put it. That is why the `tagger` container exists: it derives tags
*from* the path, so the folder layout effectively becomes the metadata.

Layout, relative to the library root:

```
<Artist>/<Album>/<NN - Title>.mp3     -> artist, album, track, title
<Album>/<Artist> - <Title>.mp3        -> album from folder, artist from filename
<Artist> - <Title>.mp3                -> album "Singles"
```

The tagger **only fills empty fields** — it never overwrites a tag you set. So
renaming an already-tagged file changes nothing. To force a re-derive, clear the
fields first:

```bash
docker exec music-tagger env PYTHONPATH=/tmp/pylibs \
  python /app/clear_tags.py '/music/path/to/file.mp3'
```

`mlib` writes tags itself at approve time, so files published through the CLI
never depend on the tagger. The tagger is the safety net for hand-uploads.

## The workflow

Staging is the whole point: an agent can fetch candidates freely, and a human
still decides what lands in the library.

```bash
mlib status                                  # config + connectivity
mlib search "balkan brass band"              # candidates, nothing downloaded
mlib add <url> --artist "X" --album "Y"      # download -> staging/
mlib staged                                  # review what is waiting
mlib approve all                             # tag, publish, trigger rescan
mlib reject 2                                # discard one
mlib ls --tracks                             # what is live
mlib rm "Artist/Album/Song.mp3"              # remove from the library
```

`approve` accepts `all`, `2`, or `1,3-4`.

Nothing in `staging/` is in the library. Nothing leaves `staging/` without
`approve`. Deletes go through `mlib rm`, and Navidrome purges the DB row on the
next scan (`ND_SCANNER_PURGEMISSING: always`).

## Running as an agent

When asked to find music, work in this order:

1. `mlib search "<description>"` — get candidates.
2. Judge them. Prefer full/official uploads over live phone recordings, clips,
   reaction videos, or hour-long compilations. Check the duration looks like a
   song.
3. `mlib add` each keeper with **explicit** `--artist` / `--album` / `--title`.
   Do not rely on the auto-guess — source titles are full of junk like
   `(Official Video)` and channel names that are not the artist.
4. `mlib staged` and **report the list to the user. Stop there.**
5. Only run `mlib approve` when the user says which ones they want.

Never approve on your own initiative. Never `mlib rm` without being asked.

Match the artist/album naming already in the library (`mlib ls`) so a new track
joins an existing album instead of creating a near-duplicate.

## Two ways to run it

`mlib` picks its destination from the environment:

- **direct** — `MLIB_MUSIC_DIR` points at the library folder. Used when running
  on the server itself: files are moved into place, no network hop.
- **webdav** — `MLIB_DAV_URL` etc. Used from a laptop. Uploads over the tailnet.

`mlib status` prints which mode is active. Direct mode is preferable for bulk
work — it is faster and does not push bytes over the network twice.

Use the **tailnet IP**, not the MagicDNS name, in `MLIB_DAV_URL`. MagicDNS does
not resolve on every client (it fails on the Windows box).

## Setup

`mlib` is a console entry point from `pyproject.toml`. Without `pip install -e .`
there is no `mlib` command and you must fall back to `python -m mlib`.

```bash
cp .env.example .env      # then fill it in; .env is gitignored
pip install -e .          # installs the `mlib` console command
mlib status
```

Server-side, see `server/` — `docker-compose.yml` plus the tagger. Deploy with
`scripts/install-server.sh`.

## Conventions

- Secrets live in `.env` only. **This repo is public** — never commit a password,
  and keep `server/docker-compose.yml` reading `${UPLOAD_PASS}` from the
  server-side `.env`.
- `mlib/naming.py` and `server/tagger/tagger.py` must agree on the layout. If you
  change the convention in one, change it in the other.
- Only fetch material you are entitled to. The tool takes URLs you give it; it is
  not a catalogue.
