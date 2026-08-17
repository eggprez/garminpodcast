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
import time
from pathlib import Path
from uuid import uuid4

import httpx

from . import db, feeds
from .config import settings

log = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, read=600.0)

# How many times an episode is retried before it is left alone.
MAX_ATTEMPTS = 3

# Only one download pass runs at a time; see download_pending().
_download_lock = asyncio.Lock()
_rerun_requested = False


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
    """Download and normalise one episode. Returns True on success.

    The state change to 'downloading' doubles as a claim: it only matches a row
    that is still pending or errored, so if another pass got there first this
    returns immediately rather than downloading the same episode twice.
    """
    claimed = db.execute_count(
        "UPDATE episodes SET state = 'downloading', attempts = attempts + 1 "
        "WHERE ref_id = ? AND state IN ('pending', 'error')",
        (ref_id,),
    )
    if not claimed:
        return False

    episode = db.query_one("SELECT * FROM episodes WHERE ref_id = ?", (ref_id,))
    if episode is None or not episode["source_url"]:
        return False

    feed = db.query_one("SELECT title FROM feeds WHERE id = ?", (episode["feed_id"],))
    show = feeds.clean_text(feed["title"] if feed else "Podcast")

    # Unique per attempt: two passes must never share a scratch file, or one
    # cleaning up would delete the other's download mid-read.
    tmp_raw = settings.audio_dir / f".{ref_id}.{uuid4().hex[:8]}.raw"
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


async def _download_pass() -> int:
    """One sweep over every feed, downloading whatever is missing.

    Works newest-first towards a target of `episodes_per_feed` ready episodes
    rather than counting free slots. Counting slots meant a feed that was
    already at its quota skipped its queue entirely, so an episode that had
    failed could never be retried and sat in the library showing a stale error
    forever.
    """
    done = 0
    target = settings.episodes_per_feed

    for feed in db.query("SELECT id FROM feeds WHERE enabled = 1"):
        feed_id = feed["id"]
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM episodes WHERE feed_id = ? AND state = 'ready'",
            (feed_id,),
        )
        ready = row["n"] if row else 0

        # Look past the target so one permanently broken episode cannot keep a
        # feed short of its quota forever.
        candidates = db.query(
            "SELECT ref_id, state, attempts FROM episodes WHERE feed_id = ? "
            "ORDER BY published DESC LIMIT ?",
            (feed_id, target * 3),
        )

        for candidate in candidates:
            if ready >= target:
                break
            if candidate["state"] in ("ready", "downloading"):
                continue
            if candidate["state"] == "error" and candidate["attempts"] >= MAX_ATTEMPTS:
                continue
            if await process_episode(candidate["ref_id"]):
                ready += 1
                done += 1

        retire_unneeded(feed_id, ready >= target)

    return done


def retire_unneeded(feed_id: int, quota_met: bool) -> int:
    """Drop episodes we are never going to fetch out of the failure list.

    An episode we do not want on disk should not sit in the library showing a
    failure — nobody asked for it. 'skipped' keeps the row, so a later poll
    does not rediscover it as new, while hiding it from the library view.

    Once the feed has its quota everything still outstanding is surplus — but
    only if we never actually tried it. An episode that was attempted and
    failed stays visible as an error even when surplus, because that is a real
    result the user may want to act on rather than something to bury.

    Below quota, only what falls outside the candidate window is surplus.
    """
    if quota_met:
        return db.execute_count(
            "UPDATE episodes SET state = 'skipped', error = '' "
            "WHERE feed_id = ? AND state IN ('pending', 'error') AND attempts = 0",
            (feed_id,),
        )
    return db.execute_count(
        "UPDATE episodes SET state = 'skipped', error = '' "
        "WHERE feed_id = ? AND state IN ('pending', 'error') AND ref_id NOT IN ("
        "  SELECT ref_id FROM episodes WHERE feed_id = ? "
        "  ORDER BY published DESC LIMIT ?"
        ")",
        (feed_id, feed_id, settings.episodes_per_feed * 3),
    )


def retry_failed(ref_id: int | None = None, feed_id: int | None = None) -> int:
    """Put failed episodes back in the queue with a fresh attempt budget.

    Scoped to one episode, one show, or everything, depending on what is given.
    """
    base = "UPDATE episodes SET state = 'pending', attempts = 0, error = '' WHERE state = 'error'"
    if ref_id is not None:
        return db.execute_count(base + " AND ref_id = ?", (ref_id,))
    if feed_id is not None:
        return db.execute_count(base + " AND feed_id = ?", (feed_id,))
    return db.execute_count(base)


async def download_pending() -> int:
    """Download missing episodes, serialised across all callers.

    Adding several feeds at once used to fire one concurrent pass per feed;
    they raced over the same rows and scratch files. The lock keeps a single
    pass running, and `_rerun_requested` coalesces everything that arrives
    while it works into exactly one follow-up sweep instead of a queue of them.
    """
    global _rerun_requested

    if _download_lock.locked():
        _rerun_requested = True
        return 0

    total = 0
    async with _download_lock:
        while True:
            _rerun_requested = False
            total += await _download_pass()
            if not _rerun_requested:
                break
    return total


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

    # Scratch files from a download killed mid-flight. The age check keeps this
    # from touching a download that is still running right now.
    stale_before = time.time() - 3600
    for path in settings.audio_dir.glob(".*.raw"):
        try:
            if path.stat().st_mtime < stale_before:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass

    if removed:
        log.info("retention: removed %d file(s)", removed)
    return removed


def reset_interrupted() -> int:
    """Return episodes stranded in 'downloading' to the queue.

    A container killed mid-download leaves rows claimed by a task that no
    longer exists; without this they would never be retried.
    """
    count = db.execute_count(
        "UPDATE episodes SET state = 'pending' WHERE state = 'downloading'"
    )
    if count:
        log.info("requeued %d episode(s) interrupted by a restart", count)
    return count


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
