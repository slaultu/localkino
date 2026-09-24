# KinoPub Offline

A local web app for macOS: browse the kino.pub catalog through their official API,
watch in your browser, and **download titles for offline viewing** — made for flights.

Runs on the system Python 3 that ships with macOS. No Node.js, no npm, no build step.

## Getting it

```bash
git clone https://github.com/slaultu/localkino.git
cd localkino
./run.sh
```

macOS only, and nothing to install: it runs on the python3 that ships with the
system. `ffmpeg` is needed for downloads (`brew install ffmpeg`).

A working `client_id` / `client_secret` pair is built in, so you can sign in to
your kino.pub account straight away. If kino.pub issues you your own pair, paste
it in Settings.

## Running it

```bash
./run.sh
```

Or double-click `KinoPub Offline.command` in Finder.
The browser opens at `http://127.0.0.1:8777/` (or the next free port).

Flags: `./run.sh --port 9000 --no-browser`. Debug logging: `KP_DEBUG=1 ./run.sh`.

## Desktop icon

```bash
./tools/make_app.sh
```

Builds `KinoPub Offline.app` into `~/Applications` and puts an alias on your
Desktop. Double-click it: the server starts and your browser opens. Quit the
app (Cmd-Q, or right-click its Dock icon) to stop the server.

The bundle carries its own copy of the code, because macOS refuses
Finder-launched apps access to `~/Documents`, `~/Desktop` and `~/Downloads` -
an app reading this project folder would die on launch with "Operation not
permitted". **Re-run the script after changing the code** to refresh the
installed copy.

The icon is drawn by `tools/make_icon.py` using only the standard library.
Startup problems are logged to `~/Library/Logs/KinoPub Offline.log`.

## First run

1. Press **Sign in** (a standard OAuth 2.0 device flow).
2. The app shows a code like `ASDFGH` — open `kino.pub/device` and enter it.
3. Once you confirm, the app picks up the token by itself and opens the catalog.

Tokens live in `~/.config/kinopub-offline/tokens.json` and refresh automatically
(access tokens last an hour, refresh tokens 30 days).

## Try it without kino.pub credentials

A mock server ships with the app, reproducing the documented endpoints. It serves a
test catalog and a short video, so downloading and offline playback genuinely work:

```bash
python3 tools/mock_kinopub.py                 # terminal 1
KP_API_BASE=http://127.0.0.1:8130 ./run.sh    # terminal 2
```

In Settings put any non-empty client_id / client_secret, then press **Sign in** —
the device code is `ABCDEF` and the mock approves it after a couple of polls.

Environment variables: `KP_API_BASE` sets the API address, `KP_CONFIG_DIR` uses a
separate settings profile (handy for keeping tests away from your real one).

## What it does

**Online**
- Home: fresh, hot and popular
- Movie and series catalog with genre filter, sorting and paging
- Search (3+ characters), collections, bookmarks, a "Watching" section
- Item page: poster, plot, IMDb/Kinopoisk ratings, cast, seasons and episodes
- In-browser player with ±30s skip, speed control and subtitles; playback position
  syncs back to kino.pub (`watching/marktime`)
- **Audio track picker** for films with several dubs or an original track,
  labelled like `Russian · DUB · stereo` or `English · Orig · 5.1`
- Playback streams over HLS: a 1080p film is a ~6 GB mp4 and the CDN takes tens
  of seconds to answer a byte range deep inside one, while HLS jumps to a small
  segment - measured at ~0.2s against ~20s for the same seek
- Errors coming back from kino.pub in Russian are shown in English

**Player keyboard shortcuts**

| Key | Action |
|---|---|
| `Space` / `K` | pause / resume |
| `←` `→` or `J` `L` | ±10 seconds |
| `↑` `↓` | volume |
| `M` | mute |
| `F` | fullscreen |
| `0`–`9` | jump to 0–90% |
| `Esc` | close the player |

**Offline — the point of the app**
- **Download** with a quality picker; for series, download a whole season
- **Unwatched** queues only the episodes you have not seen yet
- Download manager: queue, progress, speed, ETA, pause / resume / cancel
- Resumes from where it stopped (HTTP Range) — stop at 40%, continue at 40%
- Auto-reconnect on flaky wifi: up to 6 attempts with growing backoff
- An incomplete file is never marked as done, so a dropped connection cannot leave
  you with a "downloaded" film that will not open on the plane
- Free space is checked before writing, so a film cannot fill your disk
- A download interrupted by quitting the app resumes on next launch; anything you
  paused on purpose stays paused
- Subtitles are downloaded alongside and converted to `.vtt` so the player reads them
- Posters are cached locally, so the library looks right with no internet
- Launching with no network opens the Offline shelf directly instead of spinning;
  no API requests are attempted while the network is down
- If the connection drops during online playback, the player notices and offers to
  reconnect, continuing from the same position rather than the start
- Bookmarks from the item page: add/remove folders, create new ones

Files are saved in a readable layout:

```
~/Movies/KinoPub/
  Title (2024)/
    Title [1080p].mp4
    Title [1080p].rus.vtt
  Series (2023)/
    Series - S01E01 - Pilot [720p].mp4
```

## Settings

| Setting | Meaning |
|---|---|
| Default quality | which quality to offer first (2160p…360p) |
| Stream type | `http` — direct MP4, plays everywhere and downloads faster. `hls*` goes through ffmpeg |
| Parallel downloads | 1–4 at a time |
| Subtitles | whether to download subtitle tracks |
| Sync | whether to push playback position back to kino.pub |

**ffmpeg** is only needed for HLS streams. Without it, leave stream type on `http`.
Install with `brew install ffmpeg`.

## Tests

```bash
./run_tests.sh
```

22 tests, about five seconds, no dependencies. They run against a throwaway
config profile and their own stub servers, so your real settings, library and
kino.pub account are never touched.

They cover the things that are painful to discover later: a download arriving
byte-identical with its subtitles converted, no `.part` file left behind, a
dead link reporting an error instead of a half file, interrupted downloads
resuming while deliberate pauses stay paused, range requests for seeking,
the disk-space margin, two workers never claiming one download, one refresh
for concurrent 401s, and the loopback guards (cross-origin writes, DNS
rebinding, form posts, path traversal).

## How it is put together

```
server.py              HTTP server: static files, JSON API, video serving with Range support
kinopub/config.py      paths and settings (~/.config/kinopub-offline)
kinopub/api.py         OAuth device flow, automatic token refresh, /v1/* calls
kinopub/store.py       thread-safe JSON store: queue + library
kinopub/downloader.py  queue, workers, Range resume, HLS via ffmpeg, subtitles
kinopub/routes.py      local API handlers
web/                   UI: index.html + app.js + styles.css, no build step, no CDN
web/vendor/hls.min.js  hls.js (Apache-2.0), vendored locally
tools/mock_kinopub.py  mock kino.pub API for trying the app without credentials
tests/test_app.py      the suite described above
tools/make_app.sh      builds the Desktop app bundle
tools/make_icon.py     draws the icon (standard library only)
```

The frontend only reaches kino.pub through the local proxy `/api/kp/<path>`, so the
token never enters the browser or appears in a tab URL.

The server listens on `127.0.0.1` only; nothing is exposed to the network.

## Limitations

- The built-in `client_id` / `client_secret` is the Kodi addon's public pair; you can swap in your own in Settings.
- Audio tracks live inside the video file. Chrome exposes no API for them, so a
  film with several tracks is played through hls.js, which can switch them.
  Downloaded files keep every track - QuickTime or VLC can pick one.
- Catalog content (titles, plots, genres) comes from kino.pub in Russian; the
  interface is English.
- The app publishes and shares nothing — it only plays your downloads locally.
