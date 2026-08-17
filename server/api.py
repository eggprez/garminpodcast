"""The watch-facing API.

Response keys are deliberately terse. Connect IQ parses JSON into a Monkey C
dictionary held entirely in the app's memory budget, so every byte of key name
is multiplied by the number of episodes returned.

  feeds:  i=id  t=title  n=ready episode count
  eps:    i=ref_id  t=title  n=show name  d=duration seconds
          s=size bytes  p=published (unix)  f=feed id
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from . import db
from .auth import require_token

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])

API_VERSION = 1
MAX_LIMIT = 50


@router.get("/ping")
async def ping() -> dict:
    """Cheap reachability + auth check used by the watch's settings screen."""
    ready = db.query_one("SELECT COUNT(*) AS n FROM episodes WHERE state = 'ready'")
    return {"v": API_VERSION, "ok": True, "eps": ready["n"] if ready else 0}


@router.get("/feeds")
async def list_feeds() -> dict:
    rows = db.query(
        "SELECT f.id, f.title, "
        "  (SELECT COUNT(*) FROM episodes e "
        "   WHERE e.feed_id = f.id AND e.state = 'ready') AS n "
        "FROM feeds f WHERE f.enabled = 1 ORDER BY f.title COLLATE NOCASE"
    )
    return {
        "v": API_VERSION,
        "feeds": [
            {"i": r["id"], "t": r["title"] or "Untitled", "n": r["n"]}
            for r in rows
            if r["n"] > 0
        ],
    }


def _episode_rows(where: str, params: tuple) -> list[dict]:
    rows = db.query(
        "SELECT e.ref_id, e.feed_id, e.title, e.duration, e.file_size, "
        "       e.published, f.title AS show "
        "FROM episodes e JOIN feeds f ON f.id = e.feed_id "
        f"WHERE e.state = 'ready' AND {where} "
        "ORDER BY e.published DESC LIMIT ?",
        params,
    )
    return [
        {
            "i": r["ref_id"],
            "f": r["feed_id"],
            "t": r["title"],
            "n": r["show"],
            "d": r["duration"],
            "s": r["file_size"],
            "p": r["published"],
        }
        for r in rows
    ]


@router.get("/feeds/{feed_id}/episodes")
async def feed_episodes(
    feed_id: int,
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
) -> dict:
    if db.query_one("SELECT id FROM feeds WHERE id = ?", (feed_id,)) is None:
        raise HTTPException(status_code=404, detail="no such feed")
    return {"v": API_VERSION, "eps": _episode_rows("feed_id = ?", (feed_id, limit))}


@router.get("/episodes")
async def latest_episodes(limit: int = Query(20, ge=1, le=MAX_LIMIT)) -> dict:
    """Newest across every feed — backs the watch's 'Latest' pseudo-playlist."""
    return {"v": API_VERSION, "eps": _episode_rows("1 = 1", (limit,))}


@router.get("/media/{ref_id}")
async def media(ref_id: int) -> FileResponse:
    row = db.query_one(
        "SELECT file_path, title FROM episodes WHERE ref_id = ? AND state = 'ready'",
        (ref_id,),
    )
    if row is None or not row["file_path"]:
        raise HTTPException(status_code=404, detail="episode not available")

    from pathlib import Path

    path = Path(row["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="file missing on disk")

    # The media_type here is load-bearing: it must match the :mediaEncoding the
    # watch declared or the download aborts with -1002.
    return FileResponse(path, media_type="audio/mpeg", filename=f"{ref_id}.mp3")
