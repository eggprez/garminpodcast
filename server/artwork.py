"""Show artwork: fetched once per feed, downscaled, and served locally.

Podcast cover art is routinely 3000x3000 and a couple of megabytes, which is
absurd for a library grid. Caching a downscaled copy also means the admin UI
does not hotlink a dozen third-party CDNs on every page load.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from . import db
from .config import settings

log = logging.getLogger(__name__)

ART_WIDTH = 400
ART_DIR_NAME = "artwork"
# Declared here rather than imported from feeds, which imports this module.
USER_AGENT = "GarminPodcast/1.0 (+https://github.com/eggprez/garminpodcast)"


def art_dir() -> Path:
    path = settings.data_dir / ART_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


async def cache_artwork(feed_id: int, image_url: str) -> str:
    """Download and downscale a feed's cover art. Returns the stored path."""
    if not image_url:
        return ""

    row = db.query_one(
        "SELECT artwork_path, image_url FROM feeds WHERE id = ?", (feed_id,)
    )
    if row is None:
        return ""

    # Already have this exact image cached.
    existing = row["artwork_path"]
    if existing and row["image_url"] == image_url and Path(existing).is_file():
        return existing

    dest = art_dir() / f"{feed_id}.jpg"
    tmp = art_dir() / f".{feed_id}.orig"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = await client.get(image_url)
            response.raise_for_status()
            tmp.write_bytes(response.content)

        # ffmpeg is already a dependency for audio, so it doubles as the
        # image resizer rather than pulling in an imaging library.
        from .media import _run

        code, _, stderr = await _run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(tmp),
            "-vf", f"scale={ART_WIDTH}:-1",
            "-frames:v", "1",
            str(dest),
        )
        if code != 0:
            raise RuntimeError(stderr.decode(errors="replace")[:200])

        db.execute(
            "UPDATE feeds SET artwork_path = ? WHERE id = ?", (str(dest), feed_id)
        )
        return str(dest)

    except Exception as exc:
        log.info("artwork for feed %s unavailable: %s", feed_id, exc)
        return ""

    finally:
        tmp.unlink(missing_ok=True)


def artwork_file(feed_id: int) -> Path | None:
    row = db.query_one("SELECT artwork_path FROM feeds WHERE id = ?", (feed_id,))
    if row is None or not row["artwork_path"]:
        return None
    path = Path(row["artwork_path"])
    return path if path.is_file() else None
