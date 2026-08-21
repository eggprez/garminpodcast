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

    `ready` is counted while walking the candidates newest-first, not from a
    global count taken up front. A global count made the quota look "met" by
    stale ready episodes further down the list, so a feed that had already
    reached its quota once would never look at its newest candidate again —
    any episode published after that point got claimed straight into
    'skipped' by retire_unneeded() without ever being attempted.

    Each show can override the server-wide `episodes_per_feed` quota and add
    a `max_age_days` cutoff on publish date (see feeds.keep_episodes /
    max_age_days). The cutoff is enforced before anything else so a backlog
    episode that is old by publish date but only recently downloaded can
    never occupy a quota slot a genuinely new episode needs.
    """
    done = 0

    for feed in db.query(
        "SELECT id, keep_episodes, max_age_days FROM feeds WHERE enabled = 1"
    ):
        feed_id = feed["id"]
        target = (
            feed["keep_episodes"]
            if feed["keep_episodes"] is not None
            else settings.episodes_per_feed
        )
        cutoff = (
            db.now() - feed["max_age_days"] * 86400
            if feed["max_age_days"] is not None
            else None
        )

        expire_stale(feed_id, cutoff)

        # Look past the target so one permanently broken episode cannot keep a
        # feed short of its quota forever.
        candidates = db.query(
            "SELECT ref_id, state, attempts FROM episodes WHERE feed_id = ? "
            "ORDER BY published DESC LIMIT ?",
            (feed_id, target * 3),
        )

        ready = 0
        for candidate in candidates:
            if ready >= target:
                break
            if candidate["state"] == "ready":
                ready += 1
                continue
            if candidate["state"] == "downloading":
                continue
            if candidate["state"] == "error" and candidate["attempts"] >= MAX_ATTEMPTS:
                continue
            if await process_episode(candidate["ref_id"]):
                ready += 1
                done += 1

        retire_unneeded(feed_id, ready >= target, target)
        expire_surplus_ready(feed_id, target)

    return done


def expire_stale(feed_id: int, cutoff: int | None) -> int:
    """Retire anything published before the show's own age cutoff.

    Applies regardless of quota: a show's `max_age_days` is a hard "never
    keep this" line, not just a tiebreaker against newer episodes. Ready
    episodes lose their file and go 'expired'; anything still waiting to be
    tried goes 'skipped' instead, the same as a permanently unwanted surplus.
    A 'downloading' row is left alone since a pass may be mid-fetch on it.
    """
    if cutoff is None:
        return 0
    removed = 0
    for row in db.query(
        "SELECT ref_id, file_path, state FROM episodes WHERE feed_id = ? "
        "AND published < ? AND state NOT IN ('expired', 'skipped', 'downloading')",
        (feed_id, cutoff),
    ):
        if row["file_path"]:
            Path(row["file_path"]).unlink(missing_ok=True)
        new_state = "expired" if row["state"] == "ready" else "skipped"
        db.execute(
            "UPDATE episodes SET state = ?, file_path = '', file_size = 0, error = '' "
            "WHERE ref_id = ?",
            (new_state, row["ref_id"]),
        )
        removed += 1
    return removed


def expire_surplus_ready(feed_id: int, target: int) -> int:
    """Retire ready episodes that a newer arrival has bumped out of the
    newest-`target` window, so "keeping newest N" (per the show page) stays
    true instead of ready episodes only ever accumulating.
    """
    rows = db.query(
        "SELECT ref_id, file_path FROM episodes WHERE feed_id = ? AND state = 'ready' "
        "ORDER BY published DESC",
        (feed_id,),
    )
    removed = 0
    for row in rows[target:]:
        if row["file_path"]:
            Path(row["file_path"]).unlink(missing_ok=True)
        db.execute(
            "UPDATE episodes SET state = 'expired', file_path = '', file_size = 0 "
            "WHERE ref_id = ?",
            (row["ref_id"],),
        )
        removed += 1
    return removed


def retire_unneeded(feed_id: int, quota_met: bool, target: int) -> int:
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
        (feed_id, feed_id, target * 3),
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


SKIPPED_RESET_FLAG = "skipped_reset_v1"


def reset_stale_skips() -> int:
    """One-time, on upgrade: give every 'skipped' episode a fresh look.

    Before the newest-first candidate walk was fixed, any episode published
    after a feed first reached its quota got claimed straight into 'skipped'
    without ever being attempted -- and 'skipped' rows are deliberately never
    reconsidered, so those episodes were stuck there permanently. Deleting or
    retrying some other episode on the show does nothing for them; only a
    reset does.

    This is safe to run broadly because nothing sets 'skipped' except the
    download pass itself (never a direct user action), so anything that is
    still genuinely surplus under the corrected logic gets marked 'skipped'
    again on the very next pass -- it is fully self-correcting. Gated behind
    a settings flag so it only ever runs once; without that it would also
    keep reviving *deliberate* long-term surplus (old backlog beyond a
    show's quota) forever, which is not the point.
    """
    if db.get_setting(SKIPPED_RESET_FLAG):
        return 0
    count = db.execute_count(
        "UPDATE episodes SET state = 'pending', error = '' WHERE state = 'skipped'"
    )
    db.set_setting(SKIPPED_RESET_FLAG, "1")
    if count:
        log.info("re-queued %d episode(s) previously stuck as 'skipped'", count)
    return count


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
