# music-library

A free, self-hosted replacement for a music subscription: your own files, on your
own box, playing on your phone with the screen off.

- **Navidrome** indexes and streams the library
- **dufs** gives you WebDAV + a web UI to get files in (iOS Files mounts it)
- a small **tagger** service fills in metadata from the folder layout
- **`mlib`**, the CLI in this repo, finds, tags and publishes tracks
- **Amperfy** on iPhone plays it — background audio, lock screen, CarPlay

Everything binds to a Tailscale address, so it is reachable from your devices and
from nowhere else. No hosting bill, no subscription.

## Why a tagger

Navidrome reads ID3 tags and ignores folder names. Files ripped from the web
usually carry no tags at all, so they land as `[Unknown Artist]`. The tagger
derives tags from the path, which makes the folder layout behave the way people
expect it to:

```
Artist/Album/01 - Title.mp3
Album/Artist - Title.mp3
Artist - Title.mp3
```

It only fills empty fields, so tags you set by hand always win.

## Quick start

```bash
git clone git@github.com:xvanov/music-library.git
cd music-library
pip install -e .            # installs the `mlib` command
cp .env.example .env        # fill in your server address and credentials
mlib status
```

Then:

```bash
mlib search "balkan brass band"          # look
mlib add <url> --artist "X" --album "Y"  # download into staging/
mlib staged                              # review
mlib approve all                         # tag + publish + rescan
```

Nothing reaches the library until you `approve` it.

### Other commands

| Command | Does |
|---|---|
| `mlib status` | show mode, staging count, Navidrome reachability |
| `mlib ls --tracks` | list the live library |
| `mlib rm "Artist/Album/Song.mp3"` | delete a track (asks first) |
| `mlib reject 2` | discard a staged file |
| `mlib scan` | force a Navidrome rescan |

## Server

`server/` holds the deployment: `docker-compose.yml` for Navidrome + dufs +
tagger, and the tagger source. Install with:

```bash
scripts/install-server.sh
```

It expects Docker and a `server/.env` (see `server/.env.example`).

To reach it from an iPhone's Files app you need HTTPS, which Tailscale provides
free:

```bash
tailscale serve --bg --https=8445 http://TAILNET_IP:4534
```

Then in Files → Connect to Server, use `your-host.your-tailnet.ts.net:8445`.

## Mobile data

Navidrome transcodes server-side. Set Amperfy's streaming bitrate to 96–128 kbps
for roughly 1 MB/minute, or download albums over Wi-Fi and use zero cellular data.

## Note

`mlib` fetches from URLs you hand it. Only use it for material you are entitled
to — your own uploads, public domain, Creative Commons, or things you have
bought. It is not a catalogue and does not search anyone's paid library.
