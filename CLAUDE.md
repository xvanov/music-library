# music-library

Tooling for a self-hosted music setup: **Navidrome** on a home Linux box, played
on iPhone through **Amperfy**, with a CLI (`mlib`) for finding, tagging and
publishing tracks. It is genre-neutral: the library holds whatever you put in
it, and the examples below come from a real library (Balkan brass, Soviet
marches, Cossack choirs) only because that is where the lessons were learned.

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
<Album>/<Artist> - <Title>.mp3        -> a collection: album from folder, artist from
                                         filename, albumartist "Various Artists"
<Artist> - <Title>.mp3                -> album "Singles"
```

The shared albumartist is what makes a collection (e.g. `jazz vocal`, `balkan`)
show as **one** album. Without it Navidrome groups by artist and a 50-track
collection becomes 50 albums. Publish collections with
`mlib add/batch --collection <album>`.

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
mlib check                                   # listen: hold non-songs and duplicates
mlib approve all                             # tag, publish, trigger rescan
mlib reject 2                                # discard one
mlib ls --tracks                             # what is live
mlib rm "Artist/Album/Song.mp3"              # remove from the library
mlib rm "..." --bad-source --reason "..."    # bad recording: reject it, want the song again
```

`approve` accepts `all`, `2`, or `1,3-4`.

## Lessons that apply to any music

Each of these happened at least once; the tooling exists to stop a repeat.

- **The upload is not the song.** A search for a title returns a news report, a
  documentary, a let's-play or a speech with that title. `mlib check` listens
  for speech.
- **Film scenes** pass as songs: the song plus dialogue and long quiet gaps.
  Prefer uploads whose title does not mention a film; flag any that do.
- **Remixes under a classic title** — an unknown "artist" offering a well-known
  piece is usually a producer's remix (phonk, hardbass, EDM, "slowed", "bass
  boosted", "AI"). Name a real ensemble or singer as the performer, or skip it.
- **Vocals in an instrumental** (and the reverse). Transcription cannot decide
  vocal vs instrumental — Whisper can hear zero words in a fully sung track.
  Go by the user's ears and by provider labels.
- **One album became fifty** — a collection published without `--collection`.
- **YouTube rate-limits bursts** ("confirm you're not a bot"): download one at a
  time with pauses; use `MLIB_COOKIES` if it persists. **Never download from any
  site in parallel.**

## `mlib check` — listen before publishing

`check` transcribes three snippets of each staged file (Whisper, optional extra:
`pip install -e .[check]`) and fingerprints the audio against the other staged
files and the whole library. Verdicts are stored in the sidecar; `approve` runs
`check` on anything unchecked and **skips held files** unless given `--anyway`.

- `speech` — >= 0.6 distinct words/s, or >= 0.45 with speech's gappy energy
  pattern. Songs, even with clear vocals, stayed under (max 0.58, not gappy).
- `duplicate` — chroma similarity >= 0.6 at the best offset, i.e. the same
  recording. Different performances of one song are *not* duplicates.
- `electronic` — >= 40% of energy below 60 Hz. Only for collections marked
  `acoustic`: bands, orchestras and choirs sit under 26% (most under 5%); the
  one electronic remix that got through scored 61%. A techno or hip-hop
  collection would be held wholesale, so it is off by default.
- `length` — under 45s or over 15min.

**Language.** Whisper's language comes from `--lang`, else the collection's
setting, else `MLIB_CHECK_LANG`, else auto-detect. Set it for a collection when
you know it — detection on singing is unreliable:

```bash
mlib catalog collection "chanson" --lang fr
mlib catalog collection "sov instrumental" --lang ru --acoustic
mlib check --lang en 3            # one-off override
```

The speech thresholds were calibrated on Russian; other languages are unmeasured,
so read a near-the-line `speech` verdict yourself.

Library fingerprints are cached in `~/.cache/mlib/fingerprints`, so only the
first run downloads the library. The full process is the `add-music` skill.

Nothing in `staging/` is in the library. Nothing leaves `staging/` without
`approve`. Deletes go through `mlib rm`, and Navidrome purges the DB row on the
next scan (`ND_SCANNER_PURGEMISSING: always`).

## Songs vs sources — the catalog

A library file is one *source* (a YouTube upload, an archive file...) of one
*version* of one *song* (a work, e.g. "Take Five"). `catalog/songs.json` records
all three, so a song survives its source: when the audio is bad, a better
recording turns up, or an upload dies, the version is re-sourced, and every
source ever rejected is remembered and refused.

```bash
mlib songs --theme jazz --missing vocal      # what still needs finding
mlib song "Take Five"                        # versions + every source and why it failed
mlib source add "Take Five" instrumental <url> --performer "..."  # a candidate
mlib source find "Take Five" instrumental    # search a provider, record the best 3
mlib fill "Take Five" instrumental           # stage the top candidate
mlib replace "Take Five" instrumental --next --reason "bad audio"   # swap a live one
mlib replace "Take Five" instrumental --url <url> --reason "..."
```

- **Versions.** Where music comes in both, a song has `vocal` and/or
  `instrumental` (a march sung by a choir and played by a band). Everything else
  has one plain `main` version. Absent vocal/instrumental only counts as
  `--missing` for a song that has the pair.
- **Themes.** Song id is `<theme>/<normalized title>`. Theme and version come
  from the collection: `jazz vocal` -> `jazz` + `vocal`; `balkan` -> `balkan` +
  `main`; an artist album or a single -> `other` + `main`.
- Titles match on casefold, ё->е, no punctuation — nothing cleverer. "Марш Варяг"
  and "Варяг" are one work only because the song lists it in `aliases`.
  `catalog import` merges songs named by an alias and prints near-matches it did
  **not** merge; decide those by hand.
- The performer belongs to the source; library tags come from the current one.
- `add` records a staged source (refuses one already rejected/replaced/removed
  unless `--anyway`); `approve` makes it live, and for a `replace` publishes the
  new file, deletes the old path and marks the old source `replaced`; `reject
  [--reason]` marks it `rejected` (default reason: the `check` verdict).
- Removing a live file — two meanings, pick the right one:
  - `rm --bad-source --reason "..."` — **the recording is bad** (wrong audio,
    vocals in an instrumental, a remix). Rejects the source, sets the version back
    to `wanted`. **The default whenever the user reports a bad recording.**
  - `rm --reason "..."` — **the song is not wanted** (off-theme). Marks the
    version `removed`. Both keep the history.
- `mlib catalog import [--history TSV] [--picks GLOB]` rebuilds/refreshes it from
  the live listing, staging sidecars and pick files. Re-runnable: sources merge by
  id, states only move forward (a rejection is never undone by an import), and it
  syncs `live`/`staged` with reality.
- Version status `none-exists` (set by hand in the JSON) stops `--missing`
  nagging about a version that was never recorded.
- Collection options for `check` live under `collections` in the same file
  (`mlib catalog collection`).
- Concurrent writers take turns: every catalog write holds `catalog/songs.json.lock`.

## Sources — the providers

`mlib/providers.py`: `mlib search --provider`, `mlib add <url>` (provider detected
from the URL), `source find --provider`, `fill/replace --provider`.

| Provider | Role | Notes |
|---|---|---|
| `youtube` | **the default**, and the fallback for any URL yt-dlp can open | Rate-limits bursts; see the lessons above. |
| `sovmusic` | optional example: Soviet records, 1930s on | Labels each hit with year, vocal/instr (no lyricist credit = instrumental), film excerpt, foreign-language cover; `source find` uses them to drop junk and ranks the oldest first. |
| `archive` | optional example: archive.org 78s and LPs | Search understands **English or transliterated** titles only. An album item needs the file URL (`/download/<item>/<file>`). |

**Adding a provider** (Bandcamp, a label's archive...) is one class in
`providers.py` — `parse(url)`, `search(query, limit)`, `download(key, url, dest)`,
optionally `canonical()` and `probe()` — plus one line in `REGISTRY`. The module
docstring has the template; sovmusic and archive.org are the worked examples.
A provider must name staged files `<provider>-<id>.<ext>` (mlib then wipes every
tag; other sites' tags are often mis-encoded) and must serialize its requests.

**sovmusic throttle — a hard rule.** The site bans the IP or subnet of any client
that opens parallel connections. Every request goes through one cross-process
lock (`~/.cache/mlib/sovmusic.lock`) and starts at least 4 s after the previous
one finished (`~/.cache/mlib/sovmusic.last`), across separate mlib runs. Never
parallelize sovmusic requests or bypass `providers.sovmusic_get`. HTTP only
(HTTPS fails); files are `/m/<id>.mp3`, never the 32 kbps `/m32/`. Its MP3s carry
cp1251 mojibake tags (comments, TXXX, ID3v1) — the reason non-YouTube files are
wiped at download and again at approve.

Rights: the user approved sovmusic and archive.org. They are archives of old
records, not licensed services — prefer pre-1950 recordings where possible.

## `mlib batch` — a known list, zero tokens

When the user already knows artist and title, there is no judgement left to make,
so **do not send the list to a model**. `mlib batch` resolves each line by string
similarity and duration sanity:

```bash
mlib batch songs.txt --dry-run     # show what it matched, download nothing
mlib batch songs.txt               # stage the confident matches
```

Input is one `Artist - Title` per line, optionally `:: Album`. A **bare title**
also works - list decoration (`1.`, `-`, `*`, quotes, brackets) is stripped, and
the missing artists are then resolved for the whole list in **one** batched model
call (`--no-assist` to skip it, `--dry-run` to preview first).

Two things to watch when using bare titles:

- Attribution describes *what was matched*, not what you meant. If the search
  lands on a different song with a similar name, the model will faithfully
  identify that other song. Always `--dry-run` a bare-title list and read the
  right-hand column.
- The real title replaces yours, so "Djurdjevdan" becomes
  "Đurđevdan je, a ja nisam s onom koju volim". That is usually what you want,
  but it is a rename.

Adding the artist to a line (`Goran Bregovic - Mesecina`) removes both risks and
costs nothing. Anything scoring
below `--threshold` (default 0.62) is **reported, not guessed at** — the point is
that a bad match is worse than no match, since a wrong file is tedious to find
and remove later.

Forty songs costs nothing and takes one command. Reach for `auto` only when the
request is a description rather than a list.

## `mlib auto` — the one-shot LLM path

```bash
mlib auto "balkan brass bands, single songs only" --limit 3
mlib auto "..." --dry-run     # filtering only, no model call, costs nothing
```

Pipeline: yt-dlp search (free) -> deterministic prefilter (free) -> **one** model
call returning JSON -> download to staging. It never publishes; `approve` is
still yours.

Token discipline matters here because this runs on a $20 ChatGPT plan:

- The prefilter in `mlib/agent.py` drops anything outside 60-600s and anything
  matching `REJECT` (full albums, mixes, live sets, reactions). Rejecting a
  62-minute compilation costs zero tokens; asking a model about it does not.
- Only ~15 shortlisted candidates are sent, one line each (~470 tokens).
- Codex bills its own harness overhead (~4k tokens) per invocation regardless of
  prompt size, so **one call per request** is the whole game. Never loop the model
  over candidates one at a time.
- `_run_backend` runs Codex with `--cd` into an empty temp dir so it does not read
  this repo's files into context, and `--output-schema` pins the reply shape.

Backend is `MLIB_LLM_CMD` (default: `codex`, then `claude`). Any CLI that reads a
prompt on stdin and prints text works.

## Running as an agent (interactively)

When asked to find music, work in this order:

1. `mlib songs --missing any` / `mlib song "<title>"` — what is already live,
   staged or rejected. Then search the provider that fits (YouTube by default).
2. Judge the candidates. Prefer full/official uploads over live phone recordings,
   clips, reaction videos, or hour-long compilations. Check the duration looks
   like a song. Apply the lessons above.
3. `mlib add` each keeper with **explicit** `--artist` / `--album` / `--title`
   (or `source find` + `fill`). Do not rely on the auto-guess — source titles are
   full of junk like `(Official Video)` and channel names that are not the artist.
4. `mlib check`, then `mlib staged`, and **report the list to the user** —
   including anything held and why. **Stop there.**
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
  server-side `.env`. No personal data beyond `catalog/songs.json` (song names
  and source URLs, which the user is fine publishing).
- `mlib/naming.py` and `server/tagger/tagger.py` must agree on the layout. If you
  change the convention in one, change it in the other.
- Windows is case-insensitive: two library files differing only in case
  ("Армии"/"армии") collide when downloaded locally. Key local copies by index.
- Only fetch material you are entitled to. The tool takes URLs you give it; it is
  not a catalogue.
