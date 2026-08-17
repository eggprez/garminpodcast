"""Episode acquisition: download, probe, and conditionally re-encode.

Garmin's media pipeline is fussy in two specific ways this module exists to
work around:

  1. The download request declares an encoding up front and the response's
     Content-Type must agree, or `makeWebRequest` fails with -1002
     (UNSUPPORTED_CONTENT_TYPE_IN_RESPONSE). Normalising everything to MP3
     means the watch can always ask for ENCODING_MP3.
  2. Embedded cover art and exotic ID3 frames waste watch storage and have
     been observed to crash the device's tag parser, so tags are always
     rebuilt from scratch with a plain ASCII title/artist.

Re-encoding is the expensive part, so in "auto" mode it only happens when the
source is not already an acceptable MP3. Everything else is a stream copy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

import httpx

from . import db, feeds
from .config import settings

log = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, read=600.0)


async def _run(*args: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


async def probe(path: Path) -> dict:
    """Return {codec, bitrate_kbps, channels, duration} for an audio file."""
    code, stdout, stderr = await _run(
        "ffprobe",
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,bit_rate,channels:format=duration,bit_rate",
        "-of", "json",
        str(path),
    )
    if code != 0:
        raise RuntimeError(f"ffprobe failed: {stderr.decode(errors='replace')[:200]}")

    data = json.loads(stdout or b"{}")
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}

    bitrate = stream.get("bit_rate") or fmt.get("bit_rate") or "0"
    try:
        bitrate_kbps = int(bitrate) // 1000
    except (TypeError, ValueError):
        bitrate_kbps = 0

    try:
        duration = int(float(fmt.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0

    return {
        "codec": (stream.get("codec_name") or "").lower(),
        "bitrate_kbps": bitrate_kbps,
        "channels": int(stream.get("channels") or 0),
        "duration": duration,
    }


def needs_reencode(info: dict) -> bool:
    if settings.transcode_mode == "always":
        return True
    if settings.transcode_mode == "never":
        return False
    if info["codec"] != "mp3":
        return True
    # A reported bitrate of 0 means ffprobe could not work it out; re-encoding
    # is the safe choice since we cannot rule out something oversized.
    if info["bitrate_kbps"] == 0 or info["bitrate_kbps"] > settings.max_bitrate_kbps:
        return True
    return False


async def _download(url: str, dest: Path) -> None:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=DOWNLOAD_TIMEOUT,
        headers={"User-Agent": feeds.USER_AGENT},
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with dest.open("wb") as handle:
                async for chunk in response.aiter_bytes(64 * 1024):
                    handle.write(chunk)


async def _encode(src: Path, dest: Path, title: str, artist: str, reencode: bool) -> None:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    # -map 0:a:0 drops any embedded artwork stream along with everything else
    # that is not the first audio track.
    args += ["-map", "0:a:0", "-map_metadata", "-1", "-vn"]
    if reencode:
        args += [
            "-c:a", "libmp3lame",
            "-b:a", f"{settings.target_bitrate_kbps}k",
            "-ac", "1",
            "-ar", "44100",
        ]
    else:
        args += ["-c:a", "copy"]
    args += [
        "-id3v2_version", "3",
        "-write_xing", "1",
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-f", "mp3",
        str(dest),
    ]

    code, _, stderr = await _run(*args)
    if code != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='replace')[:300]}")


async def process_episode(ref_id: int) -> bool:
    """Download and normalise one pending episode. Returns True on success."""
    episode = db.query_one("SELECT * FROM episodes WHERE ref_id = ?", (ref_id,))
    if episode is None or not episode["source_url"]:
        return False

    feed = db.query_one("SELECT title FROM feeds WHERE id = ?", (episode["feed_id"],))
    show = feeds.clean_text(feed["title"] if feed else "Podcast")

    db.execute("UPDATE episodes SET state = 'downloading' WHERE ref_id = ?", (ref_id,))

    tmp_raw = settings.audio_dir / f".{ref_id}.raw"
    final = settings.audio_dir / f"{ref_id}.mp3"

    try:
        await _download(episode["source_url"], tmp_raw)
        info = await probe(tmp_raw)
        reencode = needs_reencode(info)
        log.info(
            "episode %s: codec=%s %dkbps -> %s",
            ref_id, info["codec"], info["bitrate_kbps"],
            "re-encode" if reencode else "stream copy",
        )
        await _encode(tmp_raw, final, feeds.clean_text(episode["title"]), show, reencode)

        size = final.stat().st_size
        duration = episode["duration"] or info["duration"]
        db.execute(
            "UPDATE episodes SET state = 'ready', file_path = ?, file_size = ?, "
            "duration = ?, downloaded_at = ?, error = '' WHERE ref_id = ?",
            (str(final), size, duration, db.now(), ref_id),
        )
        return True

    except Exception as exc:
        log.warning("episode %s failed: %s", ref_id, exc)
        final.unlink(missing_ok=True)
        db.execute(
            "UPDATE episodes SET state = 'error', error = ? WHERE ref_id = ?",
            (str(exc)[:300], ref_id),
        )
        return False

    finally:
        tmp_raw.unlink(missing_ok=True)


async def download_pending() -> int:
    """Download the newest pending episodes, respecting the per-feed cap."""
    done = 0
    for feed in db.query("SELECT id FROM feeds WHERE enabled = 1"):
        ready = db.query_one(
            "SELECT COUNT(*) AS n FROM episodes WHERE feed_id = ? AND state = 'ready'",
            (feed["id"],),
        )
        slots = settings.episodes_per_feed - (ready["n"] if ready else 0)
        if slots <= 0:
            continue
        pending = db.query(
            "SELECT ref_id FROM episodes WHERE feed_id = ? AND state = 'pending' "
            "ORDER BY published DESC LIMIT ?",
            (feed["id"], slots),
        )
        for row in pending:
            if await process_episode(row["ref_id"]):
                done += 1
    return done


def purge_expired() -> int:
    """Delete audio older than the server retention window, plus orphan files."""
    cutoff = db.now() - settings.retention_days * 86400
    removed = 0

    for row in db.query(
        "SELECT ref_id, file_path FROM episodes "
        "WHERE state = 'ready' AND downloaded_at > 0 AND downloaded_at < ?",
        (cutoff,),
    ):
        Path(row["file_path"]).unlink(missing_ok=True)
        db.execute(
            "UPDATE episodes SET state = 'expired', file_path = '', file_size = 0 "
            "WHERE ref_id = ?",
            (row["ref_id"],),
        )
        removed += 1

    known = {
        Path(r["file_path"]).name
        for r in db.query("SELECT file_path FROM episodes WHERE file_path != ''")
    }
    for path in settings.audio_dir.glob("*.mp3"):
        if path.name not in known:
            path.unlink(missing_ok=True)
            removed += 1

    if removed:
        log.info("retention: removed %d file(s)", removed)
    return removed


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
