"""RSS polling: fetch feeds, reconcile episodes into the database."""

from __future__ import annotations

import logging
import re
import unicodedata

import feedparser
import httpx

from . import artwork, db
from .config import settings

log = logging.getLogger(__name__)

USER_AGENT = "GarminPodcast/1.0 (+https://github.com/eggprez/garminpodcast)"
AUDIO_HINTS = ("audio/", "video/mp4")  # some feeds mislabel MP3s as video/mp4


def parse_duration(raw: str | None) -> int:
    """Accept the three shapes itunes:duration shows up in: S, MM:SS, HH:MM:SS."""
    if not raw:
        return 0
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    if not all(p.strip().isdigit() for p in parts if p.strip()):
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part or 0)
    return seconds


def _pick_enclosure(entry) -> tuple[str, str] | None:
    """Return (url, mime) for the entry's audio enclosure, if any."""
    for enc in getattr(entry, "enclosures", []) or []:
        url = enc.get("href") or enc.get("url") or ""
        mime = (enc.get("type") or "").lower()
        if url and (not mime or any(h in mime for h in AUDIO_HINTS)):
            return url, mime
    for link in getattr(entry, "links", []) or []:
        mime = (link.get("type") or "").lower()
        if link.get("rel") == "enclosure" and any(h in mime for h in AUDIO_HINTS):
            return link.get("href", ""), mime
    return None


def _publisher(feed) -> str:
    """The show's publisher, however this particular feed spells it.

    feedparser folds itunes:author and managingEditor into `author`, but plenty
    of feeds only carry one of the alternatives.
    """
    for key in ("author", "publisher", "itunes_author"):
        value = feed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    detail = feed.get("publisher_detail") or feed.get("author_detail") or {}
    name = detail.get("name", "") if isinstance(detail, dict) else ""
    return name.strip()[:120]


def _entry_guid(entry, url: str) -> str:
    return (getattr(entry, "id", "") or getattr(entry, "guid", "") or url).strip()


def _published(entry) -> int:
    import calendar

    for attr in ("published_parsed", "updated_parsed"):
        value = getattr(entry, attr, None)
        if value:
            return calendar.timegm(value)
    return db.now()


async def fetch_feed(url: str) -> feedparser.FeedParserDict:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return feedparser.parse(response.content)


async def refresh_feed(feed_id: int) -> int:
    """Poll one feed and insert any episodes we have not seen. Returns new count."""
    feed = db.query_one("SELECT * FROM feeds WHERE id = ?", (feed_id,))
    if feed is None:
        return 0

    try:
        parsed = await fetch_feed(feed["url"])
    except Exception as exc:  # network, DNS, bad status, malformed XML
        log.warning("feed %s refresh failed: %s", feed["url"], exc)
        db.execute(
            "UPDATE feeds SET last_checked = ?, last_error = ? WHERE id = ?",
            (db.now(), str(exc)[:300], feed_id),
        )
        return 0

    title = (parsed.feed.get("title") or feed["title"] or feed["url"]).strip()
    author = _publisher(parsed.feed)
    image = ""
    if parsed.feed.get("image"):
        image = parsed.feed["image"].get("href", "")

    db.execute(
        "UPDATE feeds SET title = ?, author = ?, image_url = ?, last_checked = ?, "
        "last_error = '' WHERE id = ?",
        (title, author, image, db.now(), feed_id),
    )

    if image:
        await artwork.cache_artwork(feed_id, image)

    # Only consider the newest N (this show's own quota, if it has one);
    # older ones would just be downloaded and then immediately retired.
    keep = feed["keep_episodes"] if feed["keep_episodes"] is not None else settings.episodes_per_feed
    entries = sorted(parsed.entries, key=_published, reverse=True)
    entries = entries[: max(keep * 2, keep)]

    new_count = 0
    for entry in entries:
        enclosure = _pick_enclosure(entry)
        if enclosure is None:
            continue
        source_url, mime = enclosure
        guid = _entry_guid(entry, source_url)
        existing = db.query_one(
            "SELECT ref_id FROM episodes WHERE feed_id = ? AND guid = ?",
            (feed_id, guid),
        )
        if existing:
            continue
        db.execute(
            "INSERT INTO episodes (feed_id, guid, title, published, duration, "
            "source_url, source_type, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                feed_id,
                guid,
                clean_text(entry.get("title", "Untitled")),
                _published(entry),
                parse_duration(entry.get("itunes_duration")),
                source_url,
                mime,
                db.now(),
            ),
        )
        new_count += 1

    log.info("feed '%s': %d new episode(s)", title, new_count)
    return new_count


def clean_text(text: str) -> str:
    """Strip characters that have historically crashed the Garmin ID3 parser.

    The copyright sign in particular is a known device-side crash trigger, and
    non-ASCII generally renders as garbage on MIP displays.

    Decomposing first means accented letters degrade to their base form
    ("naïve" -> "naive") instead of losing the character outright.
    """
    text = re.sub(r"[©℗™]", "", text or "")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip()[:120] or "Untitled"


async def refresh_all() -> int:
    rows = db.query("SELECT id FROM feeds WHERE enabled = 1")
    total = 0
    for row in rows:
        total += await refresh_feed(row["id"])
    return total


async def add_feed(url: str) -> int:
    url = url.strip()
    if not url:
        raise ValueError("feed URL is required")
    existing = db.query_one("SELECT id FROM feeds WHERE url = ?", (url,))
    if existing:
        return existing["id"]
    feed_id = db.execute(
        "INSERT INTO feeds (url, added_at) VALUES (?, ?)", (url, db.now())
    )
    await refresh_feed(feed_id)
    return feed_id
