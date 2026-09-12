"""Authentication for admin MCP Streamable HTTP keys (``rfmcp_`` prefix).

These keys are a dedicated trust boundary. They must never be accepted by
``get_current_user`` / billing / reseller auth.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from fastapi import HTTPException, status

from app.core.config import settings
from app.core.rate_limit import enforce_rate_limit
from app.mcp.scopes import scope_allows


MCP_KEY_PREFIX = "rfmcp_"


def _retry_after_seconds(exc: HTTPException) -> Optional[int]:
    if not exc.headers or not exc.headers.get("Retry-After"):
        return None
    try:
        return int(exc.headers["Retry-After"])
    except (TypeError, ValueError):
        return None


class MCPAuthError(Exception):
    """ASGI-friendly auth failure (status + message)."""

    def __init__(self, status_code: int, detail: str, retry_after: Optional[int] = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


@dataclass
class McpAuthContext:
    key_id: int
    name: str
    scopes: list[str]
    created_by_user_id: Optional[int]
    client_ip: Optional[str]
    enabled: bool = True

    def as_admin_auth(self) -> dict:
        """Shape expected by existing admin helpers that read ``auth['user_id']``."""
        return {
            "type": "mcp_key",
            "user_id": self.created_by_user_id,
            "username": f"mcp:{self.name}",
            "is_admin": True,
            "mcp_key_id": self.key_id,
        }


def generate_mcp_api_key() -> str:
    return f"{MCP_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_mcp_api_key(api_key: str) -> str:
    return hashlib.sha256((api_key or "").strip().encode("utf-8")).hexdigest()


def is_mcp_api_key(api_key: str) -> bool:
    return (api_key or "").strip().startswith(MCP_KEY_PREFIX)


def validate_ip_allowlist(entries: Iterable[str] | None) -> Optional[list[str]]:
    if entries is None:
        return None
    cleaned: list[str] = []
    for raw in entries:
        value = (raw or "").strip()
        if not value:
            continue
        try:
            if "/" in value:
                ipaddress.ip_network(value, strict=False)
            else:
                ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(f"Invalid IP/CIDR in allowlist: {value}") from exc
        cleaned.append(value)
    return cleaned or None


def ip_allowed(client_ip: Optional[str], allowlist: Iterable[str] | None) -> bool:
    if not allowlist:
        return True
    if not client_ip:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for raw in allowlist:
        value = (raw or "").strip()
        if not value:
            continue
        try:
            if "/" in value:
                if addr in ipaddress.ip_network(value, strict=False):
                    return True
            elif addr == ipaddress.ip_address(value):
                return True
        except ValueError:
            continue
    return False


def request_client_ip_from_headers(headers: dict[str, str], fallback: Optional[str]) -> Optional[str]:
    client_ip = fallback
    if settings.trust_x_forwarded_for:
        forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",", 1)[0].strip() or client_ip
    return client_ip


def authenticate_mcp_bearer(api_key: str, client_ip: Optional[str]) -> McpAuthContext:
    """Look up a live MCP key, enforce expiry / CIDR / rate limits, and touch last-used."""
    from app.core.database import SessionLocal
    from app.dao.mcp_api_key_dao import McpApiKeyDAO

    token = (api_key or "").strip()
    if not token or not is_mcp_api_key(token):
        raise MCPAuthError(
            status.HTTP_401_UNAUTHORIZED,
            "MCP API key required",
        )

    try:
        enforce_rate_limit(
            f"mcp:ip:{client_ip or 'unknown'}",
            settings.mcp_rate_limit_per_ip,
            settings.mcp_rate_limit_per_ip_window_seconds,
        )
    except HTTPException as exc:
        raise MCPAuthError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            str(exc.detail),
            _retry_after_seconds(exc),
        ) from exc

    db = SessionLocal()
    try:
        row = McpApiKeyDAO.get_by_api_key(db, token)
        if row is None or not row.enabled:
            raise MCPAuthError(status.HTTP_401_UNAUTHORIZED, "Invalid or disabled MCP API key")

        if row.expires_at is not None:
            expires = row.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                raise MCPAuthError(status.HTTP_401_UNAUTHORIZED, "MCP API key expired")

        if not ip_allowed(client_ip, row.ip_allowlist):
            raise MCPAuthError(status.HTTP_403_FORBIDDEN, "MCP client IP is not allowlisted")

        try:
            enforce_rate_limit(
                f"mcp:key:{row.id}",
                settings.mcp_rate_limit_per_key,
                settings.mcp_rate_limit_per_key_window_seconds,
            )
        except HTTPException as exc:
            raise MCPAuthError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                str(exc.detail),
                _retry_after_seconds(exc),
            ) from exc

        McpApiKeyDAO.touch_last_used(db, row, client_ip)
        scopes = list(row.scopes or [])
        return McpAuthContext(
            key_id=row.id,
            name=row.name,
            scopes=scopes,
            created_by_user_id=row.created_by_user_id,
            client_ip=client_ip,
            enabled=row.enabled,
        )
    finally:
        db.close()


def require_mcp_scope(ctx: McpAuthContext, required: str) -> None:
    if not scope_allows(ctx.scopes, required):
        raise MCPAuthError(
            status.HTTP_403_FORBIDDEN,
            f"MCP key lacks required scope: {required}",
        )
