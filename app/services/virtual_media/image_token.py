"""Reusable path tokens so BMCs can fetch ISOs without query strings."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from app.core.config import settings
from app.core.redis import redis_client
from app.services.virtual_media.base import VirtualMediaUnavailable

TOKEN_KEY_PREFIX = "virtual_media_image:"
MOUNT_KEY_PREFIX = "virtual_media_mount:"


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_ttl() -> int:
    return max(60, int(settings.virtual_media_token_ttl_seconds or 14400))


def virtual_media_public_base() -> str:
    base = (
        (settings.virtual_media_base_url or "").strip()
        or (settings.public_base_url or "").strip()
        or (settings.public_app_url or "").strip()
    ).rstrip("/")
    if not base:
        raise VirtualMediaUnavailable(
            "Virtual media is not configured (set VIRTUAL_MEDIA_BASE_URL, "
            "PUBLIC_BASE_URL, or PUBLIC_APP_URL so the BMC can fetch the ISO)"
        )
    return base


def image_url_for_token(token: str, filename: str) -> str:
    return (
        f"{virtual_media_public_base()}/api/virtual-media/images/"
        f"{quote(token, safe='')}/{quote(filename, safe='')}"
    )


def mint_image_token(server_id: int, filename: str) -> str:
    revoke_server_tokens(server_id)
    token = secrets.token_urlsafe(32)
    ttl = _token_ttl()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl)
    payload = {
        "server_id": str(server_id),
        "filename": filename,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    token_key = f"{TOKEN_KEY_PREFIX}{_token_id(token)}"
    redis_client.hset(token_key, mapping=payload)
    redis_client.expire(token_key, ttl)
    redis_client.set(
        f"{MOUNT_KEY_PREFIX}{server_id}",
        json.dumps({"token": token, "filename": filename}),
        ex=ttl,
    )
    return token


def lookup_image_token(token: str, filename: str) -> Optional[dict]:
    token_key = f"{TOKEN_KEY_PREFIX}{_token_id(token)}"
    data = redis_client.hgetall(token_key)
    if not data:
        return None
    if data.get("filename") != filename:
        return None
    return data


def mounted_filename(server_id: int) -> Optional[str]:
    raw = redis_client.get(f"{MOUNT_KEY_PREFIX}{server_id}")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    name = (payload.get("filename") or "").strip()
    return name or None


def revoke_server_tokens(server_id: int) -> None:
    mount_key = f"{MOUNT_KEY_PREFIX}{server_id}"
    raw = redis_client.get(mount_key)
    if raw:
        try:
            payload = json.loads(raw)
            token = payload.get("token") or ""
            if token:
                redis_client.delete(f"{TOKEN_KEY_PREFIX}{_token_id(token)}")
        except (TypeError, ValueError):
            pass
    redis_client.delete(mount_key)
