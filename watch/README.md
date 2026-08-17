# GarminPodcast — Connect IQ app

An **audio content provider** for music-capable Fenix 7 / Fenix 8 / Epix
watches. It syncs episodes from your server, deletes them after two days, and
asks where to start playing each time.

## What the system asks of this app

Connect IQ launches an audio provider in one of three modes, and `PodcastApp`
answers each with a different object:

| Mode | Method | File |
|---|---|---|
| Sync | `getSyncDelegate()` | `PodcastSyncDelegate.mc` |
| Browse | `getPlaybackConfigurationView()` | `BrowseMenu.mc` → `ResumeMenu.mc` |
| Playback | `getContentDelegate(args)` | `PodcastContentDelegate.mc` → `PodcastIterator.mc` |

## The resume prompt

Selecting an episode never plays it immediately. `ResumeMenu` offers:

- **Resume — 12:34** — the position recorded during the last listen. Hidden if
  under 30 s in, or effectively finished.
- **Start over**
- **Time left — 15 min** (and 5/10/20/30/45/60) — say how much of the episode
  *remains*; the start point is `duration − remaining`. Choices longer than the
  episode are filtered out, and the whole group is hidden if the duration is
  unknown.

Each menu item's identifier *is* the start offset in seconds, so choosing one
needs no extra lookup. The choice is handed to `Media.startPlayback()`, arrives
back at `getContentDelegate(args)`, and `PodcastIterator` applies it by
returning a **`Media.ActiveContent`** — the only mechanism Connect IQ provides
for starting mid-track. It is applied exactly once, so skipping back to the
episode later starts it from the beginning rather than silently jumping again.

Positions are recorded in `PodcastContentDelegate.onSong()`, which the player
calls with `SONG_EVENT_*` and an elapsed-seconds position. Writes are throttled
to every 10 seconds of movement — `PLAYBACK_NOTIFY` fires constantly and
persisting each tick would mean thousands of flash writes per episode — but
pause, stop and skip always flush immediately.

## The two-day retention

`Store.purgeExpired()` deletes anything downloaded more than
`Config.RETENTION_SECONDS` (172800) ago, calling `Media.deleteCachedItem()` and
dropping the bookkeeping and saved position. It runs at the start of every sync
and every time the browse menu opens, so expiry happens even if you never sync.

Episodes within a day of deletion are labelled `- expires soon` in the list.

## The id mapping, and why it exists

The server's episode id and the watch's id are **not the same**. When audio is
downloaded, Connect IQ creates its own `Media.ContentRef` and assigns the id;
the app only learns it from `data.getId()` in the download callback. That
system id is the one `onSong()` reports and the only one
`Media.getCachedContentObj()` accepts, so `Store` keys everything by it and
keeps the server id alongside purely so a re-sync knows what it already has.

## Building

Requires the Connect IQ SDK and a JRE. Put the SDK's `bin` on your `PATH`:

```bash
export PATH="$HOME/Library/Application Support/Garmin/ConnectIQ/Sdks/connectiq-sdk-mac-9.2.0-2026-06-09-92a1605b2/bin:$PATH"
```

Generate a developer key once (keep it private — never commit it):

```bash
openssl genrsa -out ~/.ssh/ciq_developer_key.pem 4096 && openssl pkcs8 -topk8 -inform PEM -outform DER -nocrypt -in ~/.ssh/ciq_developer_key.pem -out ~/.ssh/ciq_developer_key.der
```

Build for your device:

```bash
monkeyc -f monkey.jungle -o bin/GarminPodcast.prg -y ~/.ssh/ciq_developer_key.der -d fenix7
```

Run in the simulator:

```bash
connectiq && monkeydo bin/GarminPodcast.prg fenix7
```

Package for sideloading:

```bash
monkeyc -f monkey.jungle -o bin/GarminPodcast.iq -y ~/.ssh/ciq_developer_key.der -e
```

Then copy the `.prg` to `GARMIN/APPS/` on the watch over USB.

If the compiler rejects a product id in `manifest.xml`, that device is unknown
to your SDK version — remove it, or check `<SDK>/bin/devices.xml` for the
authoritative list.

## Settings

Set in **Garmin Connect Mobile → Devices → your watch → Connect IQ Apps →
Podcasts → Settings** — not on the watch, so you never type a long token on a
five-button interface.

| Setting | Notes |
|---|---|
| Server URL | `https://…`, no trailing slash needed. Must be a publicly valid certificate |
| API token | From the server's **Watch setup** page |
| Episodes to sync | 1–50, default 10 |

## Syncing

Music menu → **Podcasts** → **Sync**. Connect IQ media sync is **Wi-Fi only**;
it will not run over Bluetooth. Downloads run one at a time, and a single
failed episode is skipped rather than abandoning the run — the sync only
reports an error if nothing at all got through.

## Possible extensions

`PodcastIterator.getPlaybackProfile()` returns `null`, which leaves the player
on its default transport controls. Returning a configured `Media.PlaybackProfile`
is where you would add podcast-style ±30 s skip buttons.
