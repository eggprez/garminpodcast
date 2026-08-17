"""Preferences that can be changed at runtime from the web UI.

Environment variables set the defaults; anything stored here overrides them.
Keeping the poll interval editable in the UI matters because changing it
otherwise means editing the container definition and redeploying.
"""

from __future__ import annotations

from . import db
from .config import settings

REFRESH_KEY = "refresh_minutes"

# Below ~5 minutes you are just hammering the podcast hosts for no benefit;
# above a day the library goes stale.
MIN_REFRESH = 5
MAX_REFRESH = 1440


def refresh_minutes() -> int:
    raw = db.get_setting(REFRESH_KEY, "")
    if raw.isdigit():
        value = int(raw)
        if MIN_REFRESH <= value <= MAX_REFRESH:
            return value
    return settings.refresh_minutes


def set_refresh_minutes(value: int) -> int:
    """Store a new interval, clamped to something sane. Returns what was kept."""
    value = max(MIN_REFRESH, min(MAX_REFRESH, value))
    db.set_setting(REFRESH_KEY, str(value))
    return value
