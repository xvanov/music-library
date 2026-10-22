---
name: add-music
description: Add songs to the Navidrome library end to end - find or batch-fetch them, stage, listen for non-songs and duplicates with `mlib check`, report, and publish only what the user approves. Use when the user asks to add, find, download, upload or get music/songs/an album onto their phone or into Amperfy/Navidrome, or pastes a song list.
---

# Add music

The library is only as good as what gets approved. Two mistakes have already
happened and this process exists to stop them recurring:

1. **The file was not the song.** Searches for old marches returned news reports,
   a documentary, a let's-play and a political speech with the right title.
2. **One album became fifty.** A collection was published one-folder-per-artist,
   so Navidrome showed every track as its own album.

## Steps

### 1. Orient (free)

```bash
mlib status          # mode, staging count, Navidrome reachable?
mlib ls --local      # what is live - reuse its artist/album spelling
mlib staged          # anything left over from last time?
```

Then ask the catalog (`catalog/songs.json`) before searching anything:

```bash
mlib songs --theme sov --missing vocal   # versions nobody has found yet
mlib song "Катюша"                       # every source tried, and why it failed
```

Do not stage a song whose version is already `live` or `staged`, and do not
re-offer a source the catalog lists as `rejected`/`replaced`/`removed` - `mlib
add` refuses those anyway. A candidate you find but do not stage yet: `mlib
source add "<title>" vocal <url> --performer "..."` (plus `--theme` for a new
song), so the next run starts from it.

If `status` says Navidrome is unreachable or `MLIB_ND_PASS` is empty, say so up
front: publishing still works, but nothing appears on the phone until a rescan.

### 2. Decide the album shape

Ask only if it is unclear from the request:

- **Collection** - one album of many artists ("put the Soviet songs in sov").
  Use `--collection <album>`. Files land in `<album>/<Artist> - <Title>.mp3`,
  tagged albumartist `Various Artists`, so Navidrome shows ONE album.
  Existing collections in the library: the top-level folders of `mlib ls --local`
  whose files look like `Artist - Title.mp3` (e.g. `sov`, `balkan`).
- **Artist album** - `--artist X --album Y`, lands in `X/Y/Title.mp3`.

### 3. Fetch into staging

Pick the source by what the song is:

- **Soviet songs and marches -> sovmusic first.** Original records, and every
  hit is labelled with year, vocal/instrumental and film/foreign-language, so
  the junk YouTube serves (news reports, film scenes, remixes) is filtered for
  free:
  ```bash
  mlib source find "Катюша" vocal --provider sovmusic   # records the best 3, downloads nothing
  mlib song "Катюша"                                    # read them; reorder in the JSON if needed
  mlib fill "Катюша" vocal --provider sovmusic          # stages the top candidate
  ```
  `mlib songs --theme sov --missing vocal --find sovmusic` does the `find` step
  for every missing version, one request at a time.
  Prefer pre-1950 recordings where there is a choice.
- **Emigre Cossack choirs (Jaroff, Platoff Don Cossacks) -> archive.org.**
  Search in English or transliteration (`mlib search --provider archive "Don
  Cossack Jaroff"`). Cyrillic finds almost nothing.
- **Kuban Cossack Choir, and anything the two above lack -> YouTube**, as below.

Never run sovmusic requests in parallel (no `&`, no concurrent agents hitting
it) - the site bans the IP. mlib throttles itself; do not work around it.

- A list of known songs -> write it to a file, one `Artist - Title` per line,
  then `mlib batch songs.txt --dry-run [--collection sov]`. Read the right-hand
  column, then run it for real. No model tokens.
- A description -> `mlib search "..."`, judge, then `mlib add <url> --artist ...
  --title ... [--collection sov]` for each keeper. Always pass the names
  explicitly; source titles are junk.

Judging search results: prefer studio/official uploads, 1:30-6:00 long. Avoid
anything whose title or channel suggests news, TV, a documentary, an interview,
a game, a reaction, a parade broadcast, or "full album"/compilation.

The performer must be someone you can name as a real ensemble or a known
singer - for an instrumental, a band/orchestra/folk ensemble. An unknown solo
"artist" offering a classic march is usually a producer's remix (an "Урал Гайсин
- Священная война" turned out to be electronic). Reject titles or channels with
remix, phonk, hardbass, EDM, trap, lo-fi, synth, 8-bit, "slowed", "bass boosted",
"cover", "AI".

### 4. Check (the step that was missing)

```bash
mlib check
```

It listens to each staged file:

- transcribes three snippets with Whisper - steady distinct speech = `speech`
- fingerprints the audio and compares it to every other staged file and the
  whole library - same recording = `duplicate`
- measures sub-bass - 40%+ of the energy below 60 Hz = `electronic` (a remix,
  not a band)
- flags anything under 45s or over 15min = `length`

Flagged files are **held**: `approve` skips them. The first run fingerprints the
whole library once (cached in `~/.cache/mlib/fingerprints`), later runs are fast.

Known blind spots - say so when reporting, the user's ears are the final check:

- **Film-scene clips** (song plus dialogue and long quiet gaps, e.g. "Нам нужна
  одна победа" from *Белорусский вокзал*) can pass. Prefer uploads whose title
  does not mention a film, and flag any that do.
- **Vocal vs instrumental** cannot be decided from transcription: Whisper heard
  zero words in a fully sung "В путь!". When splitting by vocals, go by what the
  user hears, and treat a choir/ensemble performer as vocal by default.

Note what `duplicate` means: the *same recording*. Two different performances of
one song (a choir and a brass band doing "Весна Победы") are not duplicates. If
the user does not want both, that is their call - list them, do not decide.

### 5. Report and stop

Run `mlib staged` and give the user a short list: what is ok, what is held and
why (quote the transcript snippet for `speech` - it is usually obvious, e.g.
"news report about GTO norms"). **Stop. Do not approve on your own.**

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
ones they confirm.

## Bad audio in a live track

Do not `rm` and re-add - that loses the song's history. Re-source it:

```bash
mlib song "Варяг"                                        # candidates already known?
mlib source find "Варяг" vocal --provider sovmusic       # no candidates yet? find some
mlib replace "Варяг" vocal --next --reason "bad audio"   # or --url <url> --performer ...
mlib check && mlib staged                                # report, stop
```

When the user approves, `approve` publishes the new file, deletes the old one if
the path changed, and marks the old source `replaced` with the reason.

## Never

- approve, `rm`, or `--anyway` without the user saying so
- publish a collection without `--collection` (it splits into one album per artist)
- treat a same-title pair as a duplicate without the fingerprint saying so
