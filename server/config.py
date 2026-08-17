"""Runtime configuration, sourced entirely from environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    audio_dir: Path
    db_path: Path

    admin_user: str
    admin_password: str
    secret_key: str

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
        password = os.environ.get("PODCAST_ADMIN_PASSWORD", "")
        if not password:
            raise RuntimeError(
                "PODCAST_ADMIN_PASSWORD must be set. This server is meant to be "
                "reachable from the internet; refusing to start without one."
            )
        return cls(
            data_dir=data_dir,
            audio_dir=data_dir / "audio",
            db_path=data_dir / "podcasts.db",
            admin_user=os.environ.get("PODCAST_ADMIN_USER", "admin"),
            admin_password=password,
            secret_key=os.environ.get("PODCAST_SECRET_KEY") or secrets.token_urlsafe(48),
            retention_days=_int("PODCAST_RETENTION_DAYS", 14),
            episodes_per_feed=_int("PODCAST_EPISODES_PER_FEED", 5),
            refresh_minutes=_int("PODCAST_REFRESH_MINUTES", 60),
            transcode_mode=os.environ.get("PODCAST_TRANSCODE_MODE", "auto").lower(),
            max_bitrate_kbps=_int("PODCAST_MAX_BITRATE_KBPS", 128),
            target_bitrate_kbps=_int("PODCAST_TARGET_BITRATE_KBPS", 64),
            base_url=os.environ.get("PODCAST_BASE_URL", "").rstrip("/"),
        )


settings = Settings.from_env()
