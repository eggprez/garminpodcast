"""Browser-facing admin UI: feed management, status, and the watch token."""

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import artwork, db, feeds, media, prefs, scheduler
from .auth import check_admin_password, get_api_token, is_logged_in, regenerate_api_token
from .config import settings

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Crude in-process throttle. Enough to make credential stuffing through the
# reverse proxy impractical without dragging in a dependency.
_failures: dict[str, list[float]] = {}
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 300


def _throttled(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _failures.get(ip, []) if now - t < WINDOW_SECONDS]
    _failures[ip] = hits
    return len(hits) >= MAX_ATTEMPTS


def _record_failure(ip: str) -> None:
    _failures.setdefault(ip, []).append(time.time())


def _redirect(path: str = "/") -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _feed_page(row) -> str:
    """Back to the show an episode belongs to, or the library if it is gone."""
    return f"/feeds/{row['feed_id']}" if row else "/"


def _fmt_duration(seconds: int) -> str:
    if not seconds:
        return "--"
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _fmt_size(size: int) -> str:
    return f"{size / 1_048_576:.1f} MB" if size else "--"


def _fmt_ago(ts: int) -> str:
    if not ts:
        return "never"
    delta = max(0, int(time.time()) - int(ts))
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


templates.env.filters["duration"] = _fmt_duration
templates.env.filters["size"] = _fmt_size
templates.env.filters["ago"] = _fmt_ago


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if is_logged_in(request):
        return _redirect()
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    if _throttled(ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many attempts. Wait a few minutes."},
            status_code=429,
        )

    if username == settings.admin_user and check_admin_password(password):
        request.session["user"] = settings.admin_user
        return _redirect()

    _record_failure(ip)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Invalid credentials."}, status_code=401
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return _redirect("/login")


FEED_STATS = (
    "SELECT f.*, "
    "  (SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id "
    "   AND e.state = 'ready') AS ready, "
    "  (SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id "
    "   AND e.state IN ('pending', 'downloading')) AS working, "
    "  (SELECT COUNT(*) FROM episodes e WHERE e.feed_id = f.id "
    "   AND e.state = 'error') AS failed, "
    "  (SELECT COALESCE(SUM(file_size), 0) FROM episodes e WHERE e.feed_id = f.id "
    "   AND e.state = 'ready') AS bytes "
    "FROM feeds f "
)


@router.get("/", response_class=HTMLResponse)
async def library(request: Request):
    """The show library — one card per podcast."""
    if not is_logged_in(request):
        return _redirect("/login")

    shows = db.query(FEED_STATS + "ORDER BY f.title COLLATE NOCASE")
    total = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(file_size), 0) AS bytes "
        "FROM episodes WHERE state = 'ready'"
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "shows": shows,
            "total": total,
            "refresh_minutes": prefs.refresh_minutes(),
            "settings": settings,
            "ffmpeg_ok": media.ffmpeg_available(),
        },
    )


@router.get("/feeds/{feed_id}", response_class=HTMLResponse)
async def show_detail(request: Request, feed_id: int):
    """One show: its episodes and their individual states."""
    if not is_logged_in(request):
        return _redirect("/login")

    show = db.query_one(FEED_STATS + "WHERE f.id = ?", (feed_id,))
    if show is None:
        return _redirect()

    episodes = db.query(
        "SELECT * FROM episodes WHERE feed_id = ? AND state != 'skipped' "
        "ORDER BY published DESC LIMIT 100",
        (feed_id,),
    )

    return templates.TemplateResponse(
        request,
        "show.html",
        {"show": show, "episodes": episodes, "settings": settings},
    )


@router.get("/artwork/{feed_id}")
async def show_artwork(request: Request, feed_id: int):
    if not is_logged_in(request):
        return _redirect("/login")
    path = artwork.artwork_file(feed_id)
    if path is None:
        raise HTTPException(status_code=404, detail="no artwork")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not is_logged_in(request):
        return _redirect("/login")
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "refresh_minutes": prefs.refresh_minutes(),
            "min_refresh": prefs.MIN_REFRESH,
            "max_refresh": prefs.MAX_REFRESH,
            "next_run": scheduler.next_run(),
            "settings": settings,
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.post("/settings")
async def save_settings(request: Request, refresh_minutes: int = Form(15)):
    if not is_logged_in(request):
        return _redirect("/login")
    applied = prefs.set_refresh_minutes(refresh_minutes)
    scheduler.reschedule(applied)
    return _redirect("/settings?saved=1")


@router.post("/feeds/add")
async def add_feed(request: Request, url: str = Form("")):
    if not is_logged_in(request):
        return _redirect("/login")
    try:
        await feeds.add_feed(url)
        asyncio.create_task(media.download_pending())
    except Exception:
        pass  # surfaced on the dashboard via feeds.last_error
    return _redirect()


@router.post("/feeds/{feed_id}/delete")
async def delete_feed(request: Request, feed_id: int):
    if not is_logged_in(request):
        return _redirect("/login")
    for row in db.query(
        "SELECT file_path FROM episodes WHERE feed_id = ? AND file_path != ''",
        (feed_id,),
    ):
        Path(row["file_path"]).unlink(missing_ok=True)
    db.execute("DELETE FROM episodes WHERE feed_id = ?", (feed_id,))
    db.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    return _redirect()


@router.post("/feeds/{feed_id}/refresh")
async def refresh_feed(request: Request, feed_id: int):
    if not is_logged_in(request):
        return _redirect("/login")
    await feeds.refresh_feed(feed_id)
    asyncio.create_task(media.download_pending())
    return _redirect(f"/feeds/{feed_id}")


@router.post("/refresh")
async def refresh_all(request: Request):
    if not is_logged_in(request):
        return _redirect("/login")
    await feeds.refresh_all()
    asyncio.create_task(media.download_pending())
    return _redirect()


@router.post("/episodes/{ref_id}/delete")
async def delete_episode(request: Request, ref_id: int):
    if not is_logged_in(request):
        return _redirect("/login")
    row = db.query_one(
        "SELECT file_path, feed_id FROM episodes WHERE ref_id = ?", (ref_id,)
    )
    if row and row["file_path"]:
        Path(row["file_path"]).unlink(missing_ok=True)
    db.execute(
        "UPDATE episodes SET state = 'expired', file_path = '', file_size = 0 "
        "WHERE ref_id = ?",
        (ref_id,),
    )
    return _redirect(_feed_page(row))


@router.post("/episodes/{ref_id}/retry")
async def retry_episode(request: Request, ref_id: int):
    if not is_logged_in(request):
        return _redirect("/login")
    row = db.query_one("SELECT feed_id FROM episodes WHERE ref_id = ?", (ref_id,))
    media.retry_failed(ref_id)
    asyncio.create_task(media.download_pending())
    return _redirect(_feed_page(row))


@router.post("/episodes/retry-failed")
async def retry_all_failed(request: Request):
    if not is_logged_in(request):
        return _redirect("/login")
    media.retry_failed()
    asyncio.create_task(media.download_pending())
    return _redirect()


@router.post("/feeds/{feed_id}/retry-failed")
async def retry_feed_failed(request: Request, feed_id: int):
    if not is_logged_in(request):
        return _redirect("/login")
    media.retry_failed(feed_id=feed_id)
    asyncio.create_task(media.download_pending())
    return _redirect(f"/feeds/{feed_id}")


@router.get("/token", response_class=HTMLResponse)
async def show_token(request: Request):
    if not is_logged_in(request):
        return _redirect("/login")
    return templates.TemplateResponse(
        request,
        "token.html",
        {"token": get_api_token(), "base_url": settings.base_url or str(request.base_url).rstrip("/")},
    )


@router.post("/token/regenerate")
async def rotate_token(request: Request):
    if not is_logged_in(request):
        return _redirect("/login")
    regenerate_api_token()
    return _redirect("/token")
