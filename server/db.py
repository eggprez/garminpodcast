"""SQLite storage.

Single-user workload, so a lone connection behind a lock is plenty and keeps
the call sites free of async plumbing.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Iterable

from .config import settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL DEFAULT '',
    author       TEXT NOT NULL DEFAULT '',
    image_url    TEXT NOT NULL DEFAULT '',
    artwork_path TEXT NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,
    added_at     INTEGER NOT NULL,
    last_checked INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS episodes (
    -- ref_id doubles as the Garmin Media.ContentRef refId, so it must be a
    -- small stable integer.
    ref_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id        INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid           TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    published      INTEGER NOT NULL DEFAULT 0,
    duration       INTEGER NOT NULL DEFAULT 0,
    source_url     TEXT NOT NULL DEFAULT '',
    source_type    TEXT NOT NULL DEFAULT '',
    file_path      TEXT NOT NULL DEFAULT '',
    file_size      INTEGER NOT NULL DEFAULT 0,
    state          TEXT NOT NULL DEFAULT 'pending',
    error          TEXT NOT NULL DEFAULT '',
    attempts       INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    downloaded_at  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (feed_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_episodes_feed ON episodes (feed_id, published DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_state ON episodes (state);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.audio_dir.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for databases created by earlier versions."""

    def columns(table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    additions = [
        ("episodes", "attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("feeds", "author", "TEXT NOT NULL DEFAULT ''"),
        ("feeds", "artwork_path", "TEXT NOT NULL DEFAULT ''"),
    ]
    for table, column, spec in additions:
        if column not in columns(table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return connect().execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    with _lock:
        return connect().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Run a write and return lastrowid."""
    with _lock:
        conn = connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.lastrowid or 0


def execute_count(sql: str, params: Iterable[Any] = ()) -> int:
    """Run a write and return the number of rows it changed.

    Used for conditional updates that double as a claim: if the row was
    already taken by another task the update matches nothing and returns 0.
    """
    with _lock:
        conn = connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.rowcount


def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def now() -> int:
    return int(time.time())
