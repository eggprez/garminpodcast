# GarminPodcast

[![Tests](https://github.com/eggprez/garminpodcast/actions/workflows/ci.yml/badge.svg)](https://github.com/eggprez/garminpodcast/actions/workflows/ci.yml)
[![Publish container image](https://github.com/eggprez/garminpodcast/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/eggprez/garminpodcast/actions/workflows/docker-publish.yml)

A self-hosted podcast cache for Garmin music watches, in two parts:

1. **Server** — a Docker container that follows your RSS feeds, downloads new
   episodes, normalises them into watch-friendly MP3, and serves them over an
   authenticated HTTP API. Runs on TrueNAS, exposed through your reverse proxy.
2. **Watch app** — a Connect IQ *audio content provider* for Fenix 7/8 and Epix
   (Music) that syncs episodes over Wi-Fi, deletes them after **2 days**, and
   asks where to start playing every time you pick an episode.

---

## How the two halves fit together

```
 RSS feeds                 TrueNAS (Docker)                    Fenix / Epix
┌──────────┐   poll    ┌────────────────────────┐   Wi-Fi   ┌────────────────┐
│ show.rss │──────────▶│ feeds → download →     │◀─────────▶│ sync delegate  │
│ show.rss │           │ ffmpeg normalise → MP3 │  HTTPS +  │  ↓             │
└──────────┘           │ SQLite + /data/audio   │  bearer   │ media cache    │
                       │ FastAPI + web UI       │   token   │  ↓             │
                       └────────────────────────┘           │ resume prompt  │
                                                            │  ↓ playback    │
                                                            └────────────────┘
```

The server never tracks playback position — that is entirely watch-side, in
`Application.Storage`, exactly as intended.

---

## Part 1 — Server

### Quick start

Every push to `main` publishes an image, so on TrueNAS you can skip the build
entirely — set `image: ghcr.io/eggprez/garminpodcast:latest` in
`docker-compose.yml` (the line is already there, commented) and drop `build: .`:

```bash
docker pull ghcr.io/eggprez/garminpodcast:latest
```

To run from source instead:

```bash
cp .env.example .env
```

Edit `.env` (at minimum `PODCAST_ADMIN_PASSWORD` and `PODCAST_SECRET_KEY`), then:

```bash
docker compose up -d --build
```

Open `http://your-host:8080`, sign in, and add RSS URLs. The first poll runs
15 seconds after boot, then every `PODCAST_REFRESH_MINUTES`.

Generate a secret key with:

```bash
openssl rand -hex 32
```

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PODCAST_ADMIN_USER` | `admin` | Web UI username |
| `PODCAST_ADMIN_PASSWORD` | *(required)* | Web UI password; the server refuses to start without it |
| `PODCAST_SECRET_KEY` | random | Signs session cookies. Unset means a new key each restart, logging you out |
| `PODCAST_BASE_URL` | — | Public HTTPS address, shown on the watch-setup page |
| `PODCAST_COOKIE_SECURE` | `false` | Set `true` once you only reach the server over HTTPS |
| `PODCAST_RETENTION_DAYS` | `14` | How long the **server** keeps audio (the watch keeps its own copies 2 days) |
| `PODCAST_EPISODES_PER_FEED` | `5` | Ready episodes kept per show |
| `PODCAST_REFRESH_MINUTES` | `60` | Feed poll interval |
| `PODCAST_TRANSCODE_MODE` | `auto` | `auto` / `always` / `never` |
| `PODCAST_MAX_BITRATE_KBPS` | `128` | Above this, `auto` re-encodes |
| `PODCAST_TARGET_BITRATE_KBPS` | `64` | Mono MP3 target when re-encoding |

### Why the server touches the audio at all

Garmin's media pipeline is strict in two ways this project works around:

- **Content-Type must match the declared encoding.** The watch asks for
  `ENCODING_MP3`; if the response is not exactly `audio/mpeg`, the download
  fails with `-1002 UNSUPPORTED_CONTENT_TYPE_IN_RESPONSE`. Normalising
  everything to MP3 makes that mismatch impossible.
- **Tags and artwork cause trouble.** Embedded cover art wastes watch storage,
  and odd ID3 frames (the `©` character especially) have crashed the device tag
  parser. Every file gets its metadata rebuilt as plain ASCII title/artist.

In `auto` mode a clean, reasonably-sized MP3 is only *stream-copied*
(`-c:a copy`) to strip tags — cheap. Anything else (AAC, 320 kbps, unknown
bitrate) is re-encoded to mono at the target bitrate, which also cuts file size
roughly 3–5× so more episodes fit on the watch.

### TrueNAS notes

The container runs unprivileged as UID 1000. For a bind mount, the host
directory's ownership wins, so:

```bash
chown -R 1000:1000 /mnt/pool/apps/garminpodcast/data
```

### Reverse proxy

The watch requires **HTTPS with a publicly valid certificate** — Connect IQ
rejects self-signed certs outright.

Caddy:

```
podcasts.example.com {
    reverse_proxy garminpodcast:8080
}
```

nginx — note the generous timeouts and disabled buffering, since episodes are
tens of megabytes:

```nginx
location / {
    proxy_pass http://garminpodcast:8080;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    proxy_buffering off;
    proxy_read_timeout 600s;
    client_max_body_size 0;
}
```

Once HTTPS is in front, set `PODCAST_COOKIE_SECURE=true`.

### Security

- The watch authenticates with a **bearer token** sent in the `Authorization`
  header — never in a URL, so it stays out of proxy access logs. Rotate it any
  time from the **Watch setup** page.
- The web UI is a separate session login with signed cookies, `SameSite=Lax`,
  and a 5-attempts-per-5-minutes throttle per IP.
- Every `/api/v1/*` route requires the token; only `/healthz` is open.

If you expose this to the internet, consider also putting the *web UI* behind
your proxy's own auth and leaving only `/api/v1/` public — the watch never needs
the UI.

### API

All routes require `Authorization: Bearer <token>`.

| Route | Returns |
|---|---|
| `GET /api/v1/ping` | `{v, ok, eps}` — reachability and ready-episode count |
| `GET /api/v1/feeds` | Shows with at least one ready episode |
| `GET /api/v1/episodes?limit=N` | Newest episodes across all feeds |
| `GET /api/v1/feeds/{id}/episodes?limit=N` | Newest episodes for one show |
| `GET /api/v1/media/{ref_id}` | The MP3, as `audio/mpeg` |

Keys are deliberately short because Connect IQ holds the parsed JSON in the
app's memory budget: `i`=id, `t`=title, `n`=show, `d`=duration (s), `s`=size,
`p`=published, `f`=feed id.

---

## Part 2 — Watch app

See [watch/README.md](watch/README.md) for building, sideloading, and how the
resume prompt works.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Sync fails instantly, "Set the server URL and token" | App settings are empty — fill them in Garmin Connect Mobile, not on the watch |
| "Token rejected" | Token was rotated on the server; paste the new one |
| Download error `-1002` | Server returned a non-`audio/mpeg` type. Check the episode is `ready` and ffmpeg is present |
| Sync never starts | Connect IQ media sync is **Wi-Fi only**; Bluetooth will not do it |
| TLS/connection errors | Self-signed certificate. Connect IQ requires a publicly trusted cert |
| Episodes vanish after two days | Working as designed — watch-side retention |
| `ffmpeg not found` banner | Rebuild the image; ffmpeg is installed in the Dockerfile |
