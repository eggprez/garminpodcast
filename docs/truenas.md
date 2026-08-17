# Installing on TrueNAS SCALE

Written for TrueNAS SCALE 24.10 (Electric Eel) and newer, which run Docker
directly. Older Kubernetes-based releases are not covered.

Nothing here requires editing a config file or inventing a password — the
container ships with working defaults for every setting.

---

## 1. Create a dataset for the data

**Datasets → your pool → Add Dataset**

| Field | Value |
|---|---|
| Name | `garminpodcast` |
| Dataset Preset | `Generic` |

That gives you a path like `/mnt/tank/garminpodcast`. Substitute your actual
pool name everywhere below.

Episodes live here. Budget roughly **30 MB per episode** at the default 64 kbps
mono; with 5 feeds × 5 episodes kept for 14 days that is well under 5 GB.

## 2. Fix the ownership

The container runs unprivileged as **UID 1000**, and for a bind mount the
host's ownership wins. Skipping this step is the single most common cause of a
container that starts and immediately dies.

**System → Shell**:

```bash
mkdir -p /mnt/tank/garminpodcast/data
chown -R 1000:1000 /mnt/tank/garminpodcast/data
```

## 3. Install the app

**Apps → Discover Apps → ⋮ (top right) → Install via YAML**

Give it the name `garminpodcast`, then paste this into **Custom Config**,
changing only `/mnt/tank` to your pool:

```yaml
services:
  garminpodcast:
    image: ghcr.io/eggprez/garminpodcast:latest
    container_name: garminpodcast
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - /mnt/tank/garminpodcast/data:/data
```

That is the whole file. Every setting falls back to a working default, and the
session key is generated on first boot and kept in `/data/secret.key` so
restarts do not log you out.

> **TrueNAS 25.10 and newer** require the top-level `services:` key shown above.
> Use absolute host paths — relative paths like `./data` do not resolve
> predictably inside a TrueNAS custom app.

Click **Install** and wait for the app to report *Running*.

If port 8080 is already taken on your system, change the **left** number only
(for example `"8096:8080"`).

## 4. Sign in

Browse to `http://<truenas-ip>:8080` and log in with:

```
admin / changeme
```

You will see a banner warning that the default password is in use. That is
expected on a LAN. Before you expose this to the internet, change it — see
step 6.

## 5. Add your podcasts

Paste RSS feed URLs into the **Add feed** box. The server polls immediately,
then hourly. Episodes appear under *Episodes on disk* as they finish
downloading and transcoding.

Give it a few minutes on first run — it downloads up to 5 episodes per feed.

## 6. Set a real password

Once you are ready to expose the server, edit the app
(**Apps → Installed → garminpodcast → Edit**) and add an environment block:

```yaml
services:
  garminpodcast:
    image: ghcr.io/eggprez/garminpodcast:latest
    container_name: garminpodcast
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - /mnt/tank/garminpodcast/data:/data
    environment:
      PODCAST_ADMIN_PASSWORD: "pick-something-long"
      PODCAST_BASE_URL: "https://podcasts.example.com"
      PODCAST_COOKIE_SECURE: "true"
```

`PODCAST_BASE_URL` is what the **Watch setup** page shows you to type into your
phone. `PODCAST_COOKIE_SECURE` should only be `true` once you are reaching the
server over HTTPS — set it while still on plain HTTP and login will silently
fail.

The warning banner disappears once a non-default password is set.

## 7. Put it behind your reverse proxy

The watch requires **HTTPS with a publicly trusted certificate**. Connect IQ
rejects self-signed certificates outright, so a TrueNAS self-signed cert will
not work — use Let's Encrypt via your proxy.

Point the proxy at `http://<truenas-ip>:8080`. Episodes are tens of megabytes,
so raise the timeouts and turn off response buffering:

**Caddy**

```
podcasts.example.com {
    reverse_proxy <truenas-ip>:8080
}
```

**nginx**

```nginx
location / {
    proxy_pass http://<truenas-ip>:8080;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    proxy_buffering off;
    proxy_read_timeout 600s;
    client_max_body_size 0;
}
```

**Traefik** — the defaults are fine; just route the host to port 8080.

### Tightening it further

The watch only ever needs `/api/v1/`. If you want to reduce exposure, restrict
`/` (the web UI) to your LAN at the proxy and leave only `/api/v1/` public. The
watch will not notice.

## 8. Pair the watch

1. In the web UI, open **Watch setup**. It shows the server URL and API token.
2. On your phone: **Garmin Connect → Devices → your watch → Connect IQ Apps →
   Podcasts → Settings**.
3. Paste in the **Server URL** and **API token**.
4. On the watch: **Music → Podcasts → Sync**.

Sync happens over **Wi-Fi only** — Bluetooth will not do it. Put the watch on
its charger, on Wi-Fi, and it will pull down episodes.

## Updating

**Apps → Installed → garminpodcast → ⋮ → Update** pulls the newest
`:latest` image. Your data, feeds and token are all in the dataset and survive
updates untouched.

To pin a version instead, replace `:latest` with a tag like `:sha-5b501ee`.

## Backups

Everything that matters is in the dataset:

| File | Contents |
|---|---|
| `podcasts.db` | Feeds, episode metadata, the API token |
| `secret.key` | Session signing key |
| `audio/` | Downloaded MP3s — safely disposable, they re-download |

A periodic snapshot task on the dataset is plenty. You can exclude `audio/`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| App starts then stops immediately | Ownership. Re-run `chown -R 1000:1000` on the data directory |
| Can't reach the web UI | Port 8080 taken — change the host-side port in the YAML |
| Login form reloads, never signs in | `PODCAST_COOKIE_SECURE=true` while browsing over plain HTTP. Set it back to `false` |
| Episodes stuck on `pending` | Check app logs; usually the source feed is unreachable |
| `ffmpeg not found` banner | You are not running the official image — it bundles ffmpeg |
| Watch: "Token rejected" | The token was rotated. Re-copy it from **Watch setup** |
| Watch sync never starts | Connect IQ media sync is Wi-Fi only |
| Watch: TLS error | Self-signed certificate. Connect IQ needs a publicly trusted one |
