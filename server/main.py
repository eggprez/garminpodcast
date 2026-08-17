"""Application entrypoint: wiring, middleware, and the background scheduler."""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import api, db, feeds, media, web
from .auth import get_api_token
from .config import settings

logging.basicConfig(
    level=os.environ.get("PODCAST_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("garminpodcast")

scheduler = AsyncIOScheduler()


async def poll_cycle() -> None:
    """One full maintenance pass: refresh feeds, fetch new audio, expire old."""
    try:
        await feeds.refresh_all()
        await media.download_pending()
        media.purge_expired()
    except Exception:
        log.exception("scheduled poll failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    get_api_token()  # generate on first boot so the UI always has one to show
    media.reset_interrupted()

    if not media.ffmpeg_available():
        log.error("ffmpeg/ffprobe not found on PATH — downloads will fail")

    if settings.uses_default_password:
        log.warning(
            "=" * 72 + "\n"
            "  Running with the DEFAULT password (%s / %s).\n"
            "  Fine on a LAN. Set PODCAST_ADMIN_PASSWORD before exposing this\n"
            "  server to the internet through your reverse proxy.\n"
            + "=" * 72,
            settings.admin_user,
            settings.admin_password,
        )

    scheduler.add_job(
        poll_cycle,
        "interval",
        minutes=settings.refresh_minutes,
        id="poll",
        max_instances=1,
        coalesce=True,
    )
    # Kick one pass shortly after boot so a fresh container fills up without
    # waiting out a full refresh interval.
    scheduler.add_job(
        poll_cycle,
        "date",
        run_date=datetime.now() + timedelta(seconds=15),
        id="poll_initial",
    )
    scheduler.start()
    log.info(
        "ready — refresh every %dmin, server retention %dd, transcode=%s",
        settings.refresh_minutes,
        settings.retention_days,
        settings.transcode_mode,
    )
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="GarminPodcast", docs_url=None, redoc_url=None, lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="gp_session",
    max_age=14 * 86400,
    same_site="lax",
    https_only=settings.cookie_secure,
)

app.include_router(api.router)
app.include_router(web.router)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"ok": True}
