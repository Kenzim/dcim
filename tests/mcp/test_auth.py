"""MCP key auth, CIDR/rate-limit, and get_current_user rejection."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.core.auth import get_current_user
from app.core.mcp_auth import (
    MCPAuthError,
    authenticate_mcp_bearer,
    hash_mcp_api_key,
    ip_allowed,
    is_mcp_api_key,
    validate_ip_allowlist,
)
from app.dao.mcp_api_key_dao import McpApiKeyDAO


def _request(client_ip="127.0.0.1"):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "client": (client_ip, 1234),
        }
    )


def test_is_mcp_api_key_prefix():
    assert is_mcp_api_key("rfmcp_abc")
    assert not is_mcp_api_key("rsk_abc")
    assert not is_mcp_api_key("")


def test_validate_ip_allowlist():
    assert validate_ip_allowlist(["10.0.0.0/8", "192.168.1.5"]) == [
        "10.0.0.0/8",
        "192.168.1.5",
    ]
    with pytest.raises(ValueError, match="Invalid IP/CIDR"):
        validate_ip_allowlist(["not-an-ip"])
    assert ip_allowed("10.1.2.3", ["10.0.0.0/8"])
    assert not ip_allowed("11.0.0.1", ["10.0.0.0/8"])
    assert ip_allowed("1.2.3.4", None)


def test_dao_hashes_key_and_defaults_disabled(db_session):
    row = McpApiKeyDAO.create(db_session, name="ops")
    assert row.enabled is False
    assert row.plaintext_api_key.startswith("rfmcp_")
    assert row.api_key == hash_mcp_api_key(row.plaintext_api_key)
    assert row.api_key != row.plaintext_api_key
    assert McpApiKeyDAO.get_by_api_key(db_session, row.plaintext_api_key).id == row.id


def test_authenticate_live_key(db_session, mock_redis, monkeypatch, mcp_sessionlocal):
    import app.core.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    row = McpApiKeyDAO.create(
        db_session, name="live", enabled=True, scopes=["write"]
    )
    plaintext = row.plaintext_api_key

    ctx = authenticate_mcp_bearer(plaintext, "127.0.0.1")
    assert ctx.key_id == row.id
    assert ctx.scopes == ["write"]
    db_session.expire_all()
    refreshed = McpApiKeyDAO.get_by_id(db_session, row.id)
    assert refreshed.last_used_ip == "127.0.0.1"
    assert refreshed.last_used_at is not None


def test_authenticate_rejects_disabled(db_session, mock_redis, monkeypatch, mcp_sessionlocal):
    import app.core.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    row = McpApiKeyDAO.create(db_session, name="off", enabled=False)
    with pytest.raises(MCPAuthError) as exc:
        authenticate_mcp_bearer(row.plaintext_api_key, "127.0.0.1")
    assert exc.value.status_code == 401


def test_authenticate_rejects_expired(db_session, mock_redis, monkeypatch, mcp_sessionlocal):
    import app.core.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    row = McpApiKeyDAO.create(
        db_session,
        name="old",
        enabled=True,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    with pytest.raises(MCPAuthError) as exc:
        authenticate_mcp_bearer(row.plaintext_api_key, "127.0.0.1")
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_authenticate_rejects_ip(db_session, mock_redis, monkeypatch, mcp_sessionlocal):
    import app.core.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    row = McpApiKeyDAO.create(
        db_session,
        name="cidr",
        enabled=True,
        ip_allowlist=["10.0.0.0/8"],
    )
    with pytest.raises(MCPAuthError) as exc:
        authenticate_mcp_bearer(row.plaintext_api_key, "8.8.8.8")
    assert exc.value.status_code == 403


def test_authenticate_rate_limit_per_ip(db_session, mock_redis, monkeypatch, mcp_sessionlocal):
    import app.core.mcp_auth as mcp_auth_module
    import app.core.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    monkeypatch.setattr(mcp_auth_module.settings, "mcp_rate_limit_per_ip", 1)
    monkeypatch.setattr(mcp_auth_module.settings, "mcp_rate_limit_per_ip_window_seconds", 60)

    with pytest.raises(MCPAuthError) as first:
        authenticate_mcp_bearer("rfmcp_unknown", "203.0.113.9")
    assert first.value.status_code == 401

    with pytest.raises(MCPAuthError) as second:
        authenticate_mcp_bearer("rfmcp_unknown", "203.0.113.9")
    assert second.value.status_code == 429


def test_get_current_user_rejects_mcp_keys(db_session, mock_redis, monkeypatch):
    import app.core.auth as auth_module

    monkeypatch.setattr(auth_module, "redis_client", mock_redis)
    row = McpApiKeyDAO.create(db_session, name="no-fallthrough", enabled=True)
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=row.plaintext_api_key
    )
    with pytest.raises(HTTPException) as exc:
        get_current_user(
            request=_request(),
            credentials=credentials,
            auth_token=None,
            db=db_session,
        )
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid or expired token"
