"""Authentication for both surfaces: bearer token (watch) and session (web UI)."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .config import settings

TOKEN_KEY = "api_token"
_bearer = HTTPBearer(auto_error=False)


def get_api_token() -> str:
    """The watch's bearer token, generated once and persisted."""
    token = db.get_setting(TOKEN_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        db.set_setting(TOKEN_KEY, token)
    return token


def regenerate_api_token() -> str:
    token = secrets.token_urlsafe(32)
    db.set_setting(TOKEN_KEY, token)
    return token


def check_admin_password(password: str) -> bool:
    """Compare against the configured password in constant time.

    Hashing both sides first keeps the comparison length-independent, so a
    timing observer learns nothing about the real password's length.
    """
    supplied = hashlib.sha256(password.encode()).digest()
    expected = hashlib.sha256(settings.admin_password.encode()).digest()
    return hmac.compare_digest(supplied, expected)


async def require_token(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Guard every /api/v1 route the watch talks to."""
    if creds is None or not hmac.compare_digest(creds.credentials, get_api_token()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def is_logged_in(request: Request) -> bool:
    return request.session.get("user") == settings.admin_user
