---
name: add-music
description: Add songs to the Navidrome library end to end - find or batch-fetch them, stage, listen for non-songs and duplicates with `mlib check`, report, and publish only what the user approves. Use when the user asks to add, find, download, upload or get music/songs/an album onto their phone or into Amperfy/Navidrome, or pastes a song list.
---

# Add music

The library is only as good as what gets approved. Any genre works the same
way; the examples below are real cases from one library (Soviet marches, Cossack
choirs, Balkan brass), kept because each one is a mistake that already happened:

1. **The file was not the song.** Searches for old marches returned news reports,
   a documentary, a let's-play and a political speech with the right title.
2. **One album became fifty.** A collection was published one-folder-per-artist,
   so Navidrome showed every track as its own album.
3. **A remix under a classic title.** An unknown "artist" (an "Урал Гайсин -
   Священная война") turned out to be electronic.
4. **Vocals in an instrumental.** A march staged as instrumental had a choir.

## Steps

### 1. Orient (free)

```bash
mlib status          # mode, staging count, Navidrome reachable?
mlib ls --local      # what is live - reuse its artist/album spelling
mlib staged          # anything left over from last time?
```

Then ask the catalog (`catalog/songs.json`) before searching anything:

```bash
mlib songs --theme <theme> --missing any   # versions nobody has found yet
mlib song "<title>"                        # every source tried, and why it failed
```

Do not stage a song whose version is already `live` or `staged`, and do not
re-offer a source the catalog lists as `rejected`/`replaced`/`removed` - `mlib
add` refuses those anyway. A candidate you find but do not stage yet: `mlib
source add "<title>" <version> <url> --performer "..."` (plus `--theme` for a new
song), so the next run starts from it.

If `status` says Navidrome is unreachable or `MLIB_ND_PASS` is empty, say so up
front: publishing still works, but nothing appears on the phone until a rescan.

### 2. Decide the album shape and versions

Ask only if it is unclear from the request:

- **Collection** - one album of many artists ("put these in jazz vocal").
  Use `--collection <album>`. Files land in `<album>/<Artist> - <Title>.mp3`,
  tagged albumartist `Various Artists`, so Navidrome shows ONE album.
  Existing collections: the top-level folders of `mlib ls --local` whose files
  look like `Artist - Title.mp3`.
- **Artist album** - `--artist X --album Y`, lands in `X/Y/Title.mp3`.

Versions: if the music comes both sung and played (marches, standards, folk
tunes), split it into `<theme> vocal` and `<theme> instrumental` collections; the
catalog then tracks each version and `--missing` finds the gaps. Otherwise use a
plain collection (`balkan`) - every song gets one `main` version.

If the collection is in one known language, tell `check` once:
`mlib catalog collection "<name>" --lang <code>`. Add `--acoustic` for bands,
orchestras and choirs, so electronic remixes are held.

### 3. Fetch into staging

YouTube is the default source. Others exist where they are better:

| Music | Source |
|---|---|
| anything; also the fallback | `youtube` (default) |
| Soviet songs and marches | `sovmusic` first - original records, each hit labelled with year, vocal/instr and film/foreign-language, so the junk is filtered for free. Prefer pre-1950 recordings. |
| old 78s, emigre choirs (e.g. Jaroff/Platoff Don Cossacks) | `archive` - search in **English or transliteration** only |

- **A known list** -> one `Artist - Title` per line in a file, then
  `mlib batch songs.txt --dry-run [--collection ...]`. Read the right-hand
  column, then run it for real. No model tokens.
- **Songs the catalog already wants** ->
  ```bash
  mlib source find "<title>" <version> [--provider sovmusic]   # records the best 3, downloads nothing
  mlib song "<title>"                                          # read them; reorder in the JSON if needed
  mlib fill "<title>" <version> [--provider sovmusic]          # stages the top candidate
  ```
  `mlib songs --theme <theme> --missing <version> --find <provider>` runs the
  `find` step over every missing version, one request at a time.
- **A description** -> `mlib search "..." [--provider ...]`, judge, then
  `mlib add <url> --artist ... --title ... [--collection ...]` for each keeper.
  Always pass the names explicitly; source titles are junk.

**Never download in parallel** - no `&`, no concurrent agents on one site.
YouTube answers bursts with "confirm you're not a bot"; sovmusic bans the IP.
mlib serializes and throttles itself; do not work around it.

Judging search results: prefer studio/official uploads, 1:30-6:00 long. Avoid
anything whose title or channel suggests news, TV, a documentary, an interview,
a game, a reaction, a parade broadcast, a film scene, or "full album"/compilation.

The performer must be someone you can name as a real ensemble or a known
singer - for an instrumental, a band/orchestra/folk ensemble. An unknown solo
"artist" offering a well-known piece is usually a producer's remix. Reject titles
or channels with remix, phonk, hardbass, EDM, trap, lo-fi, synth, 8-bit,
"slowed", "bass boosted", "cover", "AI" - unless that is what the user asked for.

### 4. Check

```bash
mlib check              # or: mlib check 3 --lang en
```

It listens to each staged file:

- transcribes three snippets with Whisper - steady distinct speech = `speech`
- fingerprints the audio and compares it to every other staged file and the
  whole library - same recording = `duplicate`
- in collections marked `--acoustic`: 40%+ of the energy below 60 Hz =
  `electronic` (a remix, not a band)
- in an instrumental collection, transcribed words (>= 0.25/s) = `vocals` - a
  sung "march" or another piece. Whisper hears some singing as nothing, so this
  is a filter, not a guarantee
- flags anything under 45s or over 15min = `length`

Flagged files are **held**: `approve` skips them. The first run fingerprints the
whole library once (cached in `~/.cache/mlib/fingerprints`), later runs are fast.
The speech thresholds were calibrated on Russian; in other languages, read
near-the-line verdicts yourself.

Known blind spots - say so when reporting, the user's ears are the final check:

- **Film-scene clips** (song plus dialogue and long quiet gaps, e.g. "Нам нужна
  одна победа" from *Белорусский вокзал*) can pass. Prefer uploads whose title
  does not mention a film, and flag any that do.
- **Vocal vs instrumental** cannot be decided from transcription: Whisper heard
  zero words in a fully sung "В путь!". When splitting by vocals, go by what the
  user hears and by provider labels, and treat a choir/ensemble performer as
  vocal by default.
- **Diction-heavy old records trip `speech`.** Four genuine 1940s-60s songs
  (Шульженко "Синий платочек", Бунчиков "Бескозырка", ...) scored 0.49-0.74
  words/s - the same range as a poem read over music (0.70). The verdict's
  transcript settles it: song lyrics (rhyme, refrain, the title's words) vs
  prose about something else. Publish the former with `approve N --anyway` when
  the owner has opted in to auto-publish, and say so in the report.

Note what `duplicate` means: the *same recording*. Two different performances of
one song (a choir and a brass band doing one march) are not duplicates. If the
user does not want both, that is their call - list them, do not decide.

### 5. Publish what passed, report the rest

The owner of this library has opted in to auto-publish: after `mlib check`, run
`mlib approve all` - it publishes everything that passed and holds the rest.
Then tell the user what went live (counts per album) and what was held and why
(quote the transcript snippet for `speech`/`vocals` - it is usually obvious,
e.g. "news report about GTO norms"). In a library without that opt-in, report
the list and stop instead; approve only what the user picks.

### 6. Publish what they choose

```bash
mlib approve all              # publishes everything not held
mlib reject 3,5               # drop held/unwanted ones
mlib reject 6 --reason "vocals in instrumental"   # say why if check did not
mlib approve 4 --anyway       # only if the user listened and wants a held one
```

`reject` records the source as rejected in the catalog (reason defaults to the
`check` verdict), so it is never picked again. Give `--reason` whenever the user
says why - "bad audio", "film clip", "wrong song".

Then make sure it shows up:

- If approve printed `navidrome rescan triggered`, tell the user to pull to
  refresh in Amperfy.
- If it printed `navidrome NOT rescanned`, tell them to press scan in the
  Navidrome web UI (`http://<tailnet-ip>:4533`), or fill `MLIB_ND_PASS` in `.env`
  so it happens automatically next time.

## Cleaning up the live library

Same tools, applied to what is already published. Download the album to a
scratch folder, run `mlib.check.check_files` over it with `library=False`, read
the held list with the user, then `mlib rm -y "<album>/<file>.mp3"` only for the
ones they confirm (`--bad-source` if the recording, not the song, is the problem).

## Bad audio in a live track

When the user reports a bad recording ("it has vocals", "sounds wrong", "that's
a remix"), the song is still wanted - only the source is bad. Default:

```bash
mlib rm -y "<collection>/<file>.mp3" --bad-source --reason "<what the user said>"
mlib source find "<title>" <version>    # then `mlib fill` for a new one
```

That deletes the file, rejects the source (never picked again) and sets the
version back to `wanted`. Plain `mlib rm` (version `removed`) is only for "I don't
want this song at all". Never a bare `rm` and re-add - that loses the history.

To swap in a better recording while the old one stays live until approval:

```bash
mlib song "<title>"                                            # candidates already known?
mlib source find "<title>" <version>                           # no candidates yet? find some
mlib replace "<title>" <version> --next --reason "bad audio"   # or --url <url> --performer ...
mlib check && mlib staged                                      # report, stop
```

When the user approves, `approve` publishes the new file, deletes the old one if
the path changed, and marks the old source `replaced` with the reason.

## Never

- `--anyway` a held file, or plain `rm`, without the user saying so
- publish a collection without `--collection` (it splits into one album per artist)
- treat a same-title pair as a duplicate without the fingerprint saying so
- download in parallel, from any site
