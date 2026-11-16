"""Authentication for client MCP keys (``rfcmcp_`` prefix, endpoint ``/client-mcp``).

A separate trust boundary from the staff MCP keys (``rfmcp_``, ``/mcp``): own
table, own prefix, own endpoint. A client key authenticates as its owning
client user (never as an admin), is re-validated against the live user row on
every request, and is rejected by ``get_current_user`` and the staff MCP.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status

from app.core.client_permissions import PermissionKey
from app.core.config import settings
from app.core.mcp_auth import MCPAuthError, _retry_after_seconds
from app.core.rate_limit import enforce_rate_limit
from app.mcp.scopes import scope_allows

CLIENT_MCP_KEY_PREFIX = "rfcmcp_"

_MSG_INVALID_KEY = "Invalid or disabled MCP API key"


@dataclass
class ClientMcpAuthContext:
    key_id: int
    name: str
    scopes: list[str]
    user_id: int
    username: str
    email: str
    client_ip: Optional[str]

    def as_client_auth(self) -> dict:
        """Shape the client-portal handlers expect from ``get_current_user``."""
        return {
            "type": "client_mcp_key",
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "is_admin": False,
            "is_reseller": False,
            "client_mcp_key_id": self.key_id,
        }


def generate_client_mcp_api_key() -> str:
    return f"{CLIENT_MCP_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_client_mcp_api_key(api_key: str) -> str:
    return hashlib.sha256((api_key or "").strip().encode("utf-8")).hexdigest()


def is_client_mcp_api_key(api_key: str) -> bool:
    return (api_key or "").strip().startswith(CLIENT_MCP_KEY_PREFIX)


def user_is_blocked(user) -> bool:
    """True when the account may no longer act as a client (missing, staff, or switched off)."""
    if user is None or user.is_admin or user.is_reseller:
        return True
    return any(getattr(user, flag, False) for flag in ("disabled", "is_disabled", "suspended")) or (
        getattr(user, "is_active", True) is False
    )


def client_mcp_allowed_for_user(db, user) -> bool:
    """Account-level gate: usable user whose permission preset allows ``account.mcp``."""
    from app.services.client_permission_resolver import resolve_account_permission

    if user_is_blocked(user):
        return False
    return resolve_account_permission(db, user, PermissionKey.ACCOUNT_MCP)


def _rate_limit(key: str, limit: int, window: int) -> None:
    try:
        enforce_rate_limit(key, limit, window)
    except HTTPException as exc:
        raise MCPAuthError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            str(exc.detail),
            _retry_after_seconds(exc),
        ) from exc


def authenticate_client_mcp_bearer(api_key: str, client_ip: Optional[str]) -> ClientMcpAuthContext:
    """Look up a live client key, enforce expiry, owner state and rate limits, touch last-used."""
    from app.core.database import SessionLocal
    from app.dao.client_mcp_api_key_dao import ClientMcpApiKeyDAO
    from app.dao.user_dao import UserDAO

    token = (api_key or "").strip()
    if not token or not is_client_mcp_api_key(token):
        raise MCPAuthError(status.HTTP_401_UNAUTHORIZED, "Client MCP API key required")

    _rate_limit(
        f"cmcp:ip:{client_ip or 'unknown'}",
        settings.mcp_rate_limit_per_ip,
        settings.mcp_rate_limit_per_ip_window_seconds,
    )

    db = SessionLocal()
    try:
        row = ClientMcpApiKeyDAO.get_by_api_key(db, token)
        if row is None or not row.enabled:
            raise MCPAuthError(status.HTTP_401_UNAUTHORIZED, _MSG_INVALID_KEY)

        if row.expires_at is not None:
            expires = row.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                raise MCPAuthError(status.HTTP_401_UNAUTHORIZED, "MCP API key expired")

        user = UserDAO.get_by_id(db, row.user_id)
        if not client_mcp_allowed_for_user(db, user):
            raise MCPAuthError(status.HTTP_401_UNAUTHORIZED, _MSG_INVALID_KEY)

        _rate_limit(
            f"cmcp:key:{row.id}",
            settings.mcp_rate_limit_per_key,
            settings.mcp_rate_limit_per_key_window_seconds,
        )

        ClientMcpApiKeyDAO.touch_last_used(db, row, client_ip)
        return ClientMcpAuthContext(
            key_id=row.id,
            name=row.name,
            scopes=list(row.scopes or []),
            user_id=user.id,
            username=user.username,
            email=user.email,
            client_ip=client_ip,
        )
    finally:
        db.close()


def require_client_mcp_scope(ctx: ClientMcpAuthContext, required: str) -> None:
    if not scope_allows(ctx.scopes, required):
        raise MCPAuthError(
            status.HTTP_403_FORBIDDEN,
            f"MCP key lacks required scope: {required}",
        )
