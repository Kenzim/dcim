"""Launch tickets + WS sessions for bare-metal Serial-over-LAN."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from app.core.config import settings
from app.core.redis import redis_client

logger = logging.getLogger(__name__)

LAUNCH_KEY_PREFIX = "sol_launch:"
SESSION_KEY_PREFIX = "sol_session:"


class SolTicketUnavailable(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _derive_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_launch_ticket(server_id: int, expires_in: Optional[int] = None) -> str:
    ttl = max(1, int(expires_in if expires_in is not None else settings.sol_launch_ttl_seconds))
    token = secrets.token_urlsafe(32)
    key = f"{LAUNCH_KEY_PREFIX}{_derive_id(token)}"
    now = datetime.now(timezone.utc)
    redis_client.hset(
        key,
        mapping={
            "server_id": str(server_id),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        },
    )
    redis_client.expire(key, ttl)
    logger.info("Minted SOL launch ticket for server %s (ttl=%ss)", server_id, ttl)
    return token


def redeem_launch_ticket(token: str) -> Optional[int]:
    if not token:
        return None
    key = f"{LAUNCH_KEY_PREFIX}{_derive_id(token)}"
    data = redis_client.hgetall(key)
    if not data:
        return None
    if not redis_client.hsetnx(key, "consumed", "1"):
        return None
    redis_client.delete(key)
    server_id = data.get("server_id")
    return int(server_id) if server_id is not None else None


def mint_viewer_session(
    server_id: int,
    profile_id: str,
    expires_in: Optional[int] = None,
) -> dict:
    ttl = max(1, int(expires_in if expires_in is not None else settings.sol_session_ttl_seconds))
    token = secrets.token_urlsafe(32)
    key = f"{SESSION_KEY_PREFIX}{_derive_id(token)}"
    now = datetime.now(timezone.utc)
    redis_client.hset(
        key,
        mapping={
            "server_id": str(server_id),
            "profile_id": profile_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        },
    )
    redis_client.expire(key, ttl)
    logger.info("Minted SOL viewer session for server %s (ttl=%ss)", server_id, ttl)
    return {"ws_token": token, "expires_in": ttl}


def get_ws_session(token: str) -> Optional[dict]:
    if not token:
        return None
    key = f"{SESSION_KEY_PREFIX}{_derive_id(token)}"
    data = redis_client.hgetall(key)
    if not data:
        return None
    try:
        return {
            "server_id": int(data["server_id"]),
            "profile_id": data["profile_id"],
        }
    except (KeyError, ValueError):
        return None


def build_public_app_url() -> str:
    base = (settings.public_app_url or "").strip().strip("/")
    if not base:
        raise SolTicketUnavailable("Serial-over-LAN is not configured (missing public_app_url)")
    return base


def build_launch_url(token: str) -> str:
    return f"{build_public_app_url()}/sol?t={token}"


def build_relative_launch_url(token: str) -> str:
    return f"/sol?t={token}"


def build_relative_error_url(message: str) -> str:
    return f"/sol?e={quote(message)}"
