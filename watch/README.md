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

Verified against **Connect IQ SDK 9.2.0** with **OpenJDK 26**. All 15 declared
products compile; the exported package covers 25 device variants.

`monkeyc` is a Java tool, so you need a JRE:

```bash
brew install openjdk
```

Put both the JDK and the SDK's `bin` on your `PATH` (adjust the SDK version):

```bash
export PATH="/opt/homebrew/opt/openjdk/bin:$HOME/Library/Application Support/Garmin/ConnectIQ/Sdks/connectiq-sdk-mac-9.2.0-2026-06-09-92a1605b2/bin:$PATH"
```

Generate a developer key once. Keep it outside the repo — it is a private key:

```bash
mkdir -p ~/.ciq && openssl genrsa -out ~/.ciq/developer_key.pem 4096 && openssl pkcs8 -topk8 -inform PEM -outform DER -nocrypt -in ~/.ciq/developer_key.pem -out ~/.ciq/developer_key.der && chmod 600 ~/.ciq/developer_key.*
```

Build for one device:

```bash
monkeyc -f monkey.jungle -o bin/GarminPodcast.prg -y ~/.ciq/developer_key.der -d fenix7
```

Run in the simulator:

```bash
connectiq && monkeydo bin/GarminPodcast.prg fenix7
```

Build the distributable package for every supported device:

```bash
monkeyc -f monkey.jungle -o bin/GarminPodcast.iq -y ~/.ciq/developer_key.der -e
```

To sideload, copy the per-device `.prg` to `GARMIN/APPS/` on the watch over USB.

If the compiler rejects a product id, that device is unknown to your SDK
version. The authoritative list is the directory names under
`~/Library/Application Support/Garmin/ConnectIQ/Devices/`.

### Two things the compiler enforces

Worth knowing before editing the sources, because neither is obvious:

- **`hidden` is a class-member modifier only.** Module-level functions cannot
  use it — `Config` and `Store` are modules, so their functions are all plain.
- **`has` is a reserved operator** (the API-availability check), so it cannot be
  a method name. That is why the lookup is `Store.hasServerId()`.

Web-request callbacks also need full Monkey Types annotations. The audio
download hands back a `Media.ContentRef`, which is *not* in the SDK's declared
callback type, so `PodcastSyncDelegate` widens through `Lang.Object` before
narrowing to read `getId()`.

## Settings

| Setting | Notes |
|---|---|
| Server URL | `https://…`, trailing slash optional. Must be a publicly trusted certificate |
| API token | From the server's **Watch setup** page |
| Episodes to sync | 1–50, default 10 |

How you set these depends entirely on how the app got onto the watch.

### Sideloaded builds: bake them in

**Garmin only exposes Connect IQ app settings in the phone app for apps
installed from the Connect IQ Store.** A sideloaded `.prg` gets no settings
screen at all — the app appears under Connect IQ Apps but tapping into it shows
nothing to configure. A `.SET` file does appear in `/GARMIN/Apps/SETTINGS` on
the device, but it is not usefully editable by hand.

So for a sideloaded build the configuration is compiled in:

```bash
./configure.sh https://podcasts.example.com <api-token>
monkeyc -f monkey.jungle -o bin/GarminPodcast.prg -y ~/.ciq/developer_key.der -d fenix7
```

Copy the rebuilt `.prg` to `GARMIN/APPS/` and the app starts up already
pointed at your server.

`configure.sh` writes `resources-local/properties/properties.xml`, which
`monkey.jungle` layers over `resources/`. That path is git-ignored, so your
token never reaches a tracked file — worth caring about given this repo is
public. Re-run it and rebuild whenever you rotate the token.

Changing settings means rebuilding and re-copying. That is the trade-off for
not publishing to the store.

### Store installs: use the phone

If the app is ever published to the Connect IQ Store, the settings defined in
`resources/settings/settings.xml` show up under **Garmin Connect → Devices →
your watch → Connect IQ Apps → Podcasts → Settings**, and `configure.sh`
becomes unnecessary. Entering a long token on the phone beats doing it on a
five-button watch, which is why that path exists.

## Syncing

Music menu → **Podcasts** → **Sync**. Connect IQ media sync is **Wi-Fi only**;
it will not run over Bluetooth. Downloads run one at a time, and a single
failed episode is skipped rather than abandoning the run — the sync only
reports an error if nothing at all got through.

## Possible extensions

`PodcastIterator.getPlaybackProfile()` returns `null`, which leaves the player
on its default transport controls. Returning a configured `Media.PlaybackProfile`
is where you would add podcast-style ±30 s skip buttons.
