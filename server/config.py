"""Runtime configuration.

Every setting has a working default so the container runs with no environment
variables at all. The two that would normally be mandatory are handled like
this:

  * the session key is generated once and persisted under the data directory,
    so sessions survive restarts without anyone having to invent a key;
  * the admin password falls back to a well-known default, which is fine on a
    LAN but *must* be changed before the server is reachable from the internet.
    `uses_default_password` drives the warnings that say so.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ADMIN_PASSWORD = "changeme"
SECRET_KEY_FILE = "secret.key"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _persisted_secret_key(data_dir: Path) -> str:
    """Read the session key, creating it on first run.

    Kept in a file rather than the database so it is readable before SQLite is
    open, and written 0600 so other users on the host cannot forge cookies.
    """
    path = data_dir / SECRET_KEY_FILE
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass

    key = secrets.token_urlsafe(48)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(key)
        path.chmod(0o600)
    except OSError:
        # Read-only or unwritable volume: fall back to an in-memory key. The
        # server still works, it just logs everyone out on restart.
        pass
    return key


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    audio_dir: Path
    db_path: Path

    admin_user: str
    admin_password: str
    uses_default_password: bool
    secret_key: str
    cookie_secure: bool

    # How long the *server* keeps an episode on disk. The watch has its own,
    # much shorter retention (2 days) which is enforced watch-side.
    retention_days: int
    episodes_per_feed: int
    refresh_minutes: int

    # "auto" transcodes only when the source is not already a watch-friendly
    # MP3; "always" re-encodes everything; "never" only strips metadata.
    transcode_mode: str
    max_bitrate_kbps: int
    target_bitrate_kbps: int

    base_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("PODCAST_DATA_DIR", "/data"))
        password = os.environ.get("PODCAST_ADMIN_PASSWORD", "").strip()

        return cls(
            data_dir=data_dir,
            audio_dir=data_dir / "audio",
            db_path=data_dir / "podcasts.db",
            admin_user=os.environ.get("PODCAST_ADMIN_USER", "admin"),
            admin_password=password or DEFAULT_ADMIN_PASSWORD,
            uses_default_password=not password,
            secret_key=(
                os.environ.get("PODCAST_SECRET_KEY", "").strip()
                or _persisted_secret_key(data_dir)
            ),
            cookie_secure=_bool("PODCAST_COOKIE_SECURE", False),
            retention_days=_int("PODCAST_RETENTION_DAYS", 14),
            episodes_per_feed=_int("PODCAST_EPISODES_PER_FEED", 5),
            refresh_minutes=_int("PODCAST_REFRESH_MINUTES", 15),
            transcode_mode=os.environ.get("PODCAST_TRANSCODE_MODE", "auto").lower(),
            max_bitrate_kbps=_int("PODCAST_MAX_BITRATE_KBPS", 128),
            target_bitrate_kbps=_int("PODCAST_TARGET_BITRATE_KBPS", 64),
            base_url=os.environ.get("PODCAST_BASE_URL", "").rstrip("/"),
        )


settings = Settings.from_env()
