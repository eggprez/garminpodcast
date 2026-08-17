"""The background poll loop.

Lives in its own module so the web UI can reschedule it without importing the
application entrypoint, which imports the web UI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import feeds, media, prefs

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
POLL_JOB = "poll"


async def poll_cycle() -> None:
    """One full maintenance pass: refresh feeds, fetch new audio, expire old."""
    try:
        await feeds.refresh_all()
        await media.download_pending()
        media.purge_expired()
    except Exception:
        log.exception("scheduled poll failed")


def start() -> None:
    minutes = prefs.refresh_minutes()
    scheduler.add_job(
        poll_cycle,
        "interval",
        minutes=minutes,
        id=POLL_JOB,
        max_instances=1,
        coalesce=True,
    )
    # Kick one pass shortly after boot so a fresh container fills up without
    # waiting out a full interval.
    scheduler.add_job(
        poll_cycle,
        "date",
        run_date=datetime.now() + timedelta(seconds=15),
        id="poll_initial",
    )
    scheduler.start()
    log.info("polling every %d minute(s)", minutes)


def reschedule(minutes: int) -> None:
    """Apply a new interval to the running scheduler, no restart needed."""
    if scheduler.running:
        scheduler.reschedule_job(POLL_JOB, trigger="interval", minutes=minutes)
        log.info("poll interval changed to %d minute(s)", minutes)


def next_run() -> datetime | None:
    job = scheduler.get_job(POLL_JOB) if scheduler.running else None
    return job.next_run_time if job else None
