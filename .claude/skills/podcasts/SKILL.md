---
name: podcasts
description: Subscribe to, stop, or change podcasts in the Navidrome library - each show keeps its newest episodes (default 2): a new release arrives on every sync, played ones are removed, and the oldest beyond the limit is dropped. Use when the user says "I want to listen to <show>", "add/subscribe to <podcast>", "stop/unsubscribe <show>", "keep 3 of <show>", "skip the clips/trailers", or asks what podcasts are on the phone.
---

# Podcasts

Navidrome has no podcasts, so an episode is an ordinary track:
`<Show>/<Show>/<YYYY-MM-DD - Title>.mp3`, artist = album = the show, genre
`Podcast`. Per show the library holds the newest `keep` episodes (default 2).
Every hourly `mlib pod sync`:

1. deletes an episode Amperfy reported played (Navidrome playCount > 0, last
   played at least `MLIB_POD_GRACE_HOURS` ago - default 6 - and not playing now);
2. publishes any new release, whether or not the older ones were played
   (`latest` mode, the default);
3. deletes the oldest while more than `keep` are live - unless it is playing
   now or was played within the grace period (the user is partway through it).

A heard or dropped episode, or anything older than a heard one, never comes
back. `--mode heard` restores the old swap-on-play behaviour: nothing new
arrives until a live episode is played. `--keep 2` is deliberate: the episode
the user is halfway through stays while the next one lands.

The server runs `mlib pod sync` hourly (`scripts/install-podcast-timer.sh`), so
after subscribing there is nothing to schedule. The state lives in the library
itself, `.mlib/podcasts.json`, shared by the laptop (WebDAV) and the server
(direct); `catalog/podcasts.json` is a backup copy mlib rewrites after every
change, for history in the private catalog repo.

## "I want to listen to X"

```bash
mlib pod ls                              # already subscribed?
mlib pod add "<show name>"               # looks it up in Apple's podcast directory
```

`add` prints the directory matches with a `*` on the one it took and its feed
URL, subscribes, and publishes the newest `keep` episodes right away. **Read the list.**
Fan feeds, clip channels and "audio biography" feeds copy the real name; the
real one is the show's own publisher, updated this week, with hundreds of
episodes. If the `*` is wrong:

```bash
mlib pod rm "<name it took>"             # deletes the wrong episode too
mlib pod add "<show>" --pick 3           # or pass the feed URL itself
```

Then look at what the feed actually publishes (the first sync names it). If the
show mixes in trailers, clips or re-runs, filter them so they never take the
slot:

```bash
mlib pod add "<show>" --min-minutes 15                 # skip shorts and trailers
mlib pod add "<show>" --exclude "^(Trailer|Bonus)"     # skip by title (regex)
```

Watch for shows that post highlight clips or re-runs of old episodes between
new ones, often under a fixed prefix ("Best of", "Highlights") and
20-40 minutes long - too long for `--min-minutes`, so filter them by title.

A show only on YouTube: `mlib pod add "<name>" --youtube <channel or playlist URL>`.
Prefer RSS when it exists - the original audio, no transcoding, no bot checks.

## "Keep 3 of X" / change the filters

`add` on a show that is already subscribed changes only the options given:

```bash
mlib pod add "<show>" --keep 3           # the next sync fills up to 3, newest first
mlib pod add "<show>" --mode heard       # only replace what was played
mlib pod add "<show>" --exclude ""       # clear the title filter
```

## "Stop X"

```bash
mlib pod rm "<show>"                     # unsubscribe, delete its live episodes, rescan
mlib pod rm "<show>" --keep-files        # unsubscribe but leave what is there
```

## Run a cycle now

```bash
mlib pod sync                            # every show
mlib pod sync "<show>"                   # one show
```

If it warns `cannot read play counts`, `MLIB_ND_PASS` is empty or Navidrome is
down: new releases are still published, but nothing is deleted, so a show can
hold more than `keep` until Navidrome can be read. A show that
fails (feed down, download error) is logged as `FAILED` and the exit code is 1;
the other shows still run. An episode that failed 3 times is skipped for good
(`pod ls` counts them). `no podcast subscriptions` with exit 1 means mlib is
looking at a library without `.mlib/podcasts.json` - check `mlib status`. Play counts
are per Navidrome user - `MLIB_ND_USER` must be the account Amperfy logs in
with, or heard episodes never rotate. Without a rescan (`navidrome NOT
rescanned`), tell the user to press scan in the Navidrome web UI.

## Is the server timer running?

`mlib pod ls` starts with `last sync: <time> on <host> (<n>h ago)` - every sync
records itself in the shared state. More than ~2 hours old means the server
timer is not running: on the server, `systemctl --user list-timers
mlib-podcasts.timer`, `journalctl --user -u mlib-podcasts -n 50`, and
`loginctl show-user $USER -p Linger` (must be `yes`, or the timer stops at
logout). Re-run `scripts/install-podcast-timer.sh` to fix any of them.

## Afterwards

The live state is already in the library. Commit the backup copy in the
private catalog repo, from inside `catalog/` - never in the public
music-library repo:

```bash
cd catalog && git add podcasts.json && git commit -m "Podcasts: <what changed>" && git push
```

## Never

- delete episodes by hand with `mlib rm` - `sync` notices and counts it as heard,
  but `pod rm` or letting `sync` retire them keeps the intent clear
- edit `.mlib/podcasts.json` by hand while a sync runs
- run two downloads at once; sync is serial and takes a lock
- put show names or feed URLs in the public repo
