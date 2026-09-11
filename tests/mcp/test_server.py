"""Unit tests for MCP ASGI auth helpers and middleware."""

import json

import pytest

from app.dao.mcp_api_key_dao import McpApiKeyDAO
from app.mcp.server import (
    MCPAuthASGI,
    _bearer_token,
    _header_map,
    _send_json,
    get_mcp_asgi_app,
    get_mcp_starlette,
)


def test_header_map_decodes_scope_headers():
    scope = {
        "headers": [
            (b"Authorization", b"Bearer rfmcp_test"),
            (b"X-Forwarded-For", b"10.0.0.1"),
        ]
    }
    headers = _header_map(scope)
    assert headers["authorization"] == "Bearer rfmcp_test"
    assert headers["x-forwarded-for"] == "10.0.0.1"


def test_bearer_token_extracts_value():
    assert _bearer_token({"authorization": "Bearer abc123"}) == "abc123"
    assert _bearer_token({"authorization": "Basic x"}) == ""
    assert _bearer_token({}) == ""


@pytest.mark.asyncio
async def test_send_json_writes_response():
    messages = []

    async def send(message):
        messages.append(message)

    await _send_json(send, 429, "slow down", retry_after=30)
    assert messages[0]["status"] == 429
    assert any(h[0] == b"retry-after" and h[1] == b"30" for h in messages[0]["headers"])
    body = json.loads(messages[1]["body"].decode())
    assert body["detail"] == "slow down"


@pytest.mark.asyncio
async def test_mcp_auth_rejects_missing_token(mcp_sessionlocal, mock_redis, monkeypatch):
    import app.core.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    called = False

    async def inner(scope, receive, send):
        nonlocal called
        called = True

    middleware = MCPAuthASGI(inner)
    messages = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "path": "/",
        "headers": [],
        "client": ("127.0.0.1", 1234),
    }
    await middleware(scope, lambda: None, send)
    assert not called
    assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_mcp_auth_passes_valid_key(
    db_session, mcp_sessionlocal, mock_redis, monkeypatch
):
    import app.core.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    row = McpApiKeyDAO.create(db_session, name="live", enabled=True, scopes=["read"])
    seen = {}

    async def inner(scope, receive, send):
        seen["path"] = scope["path"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = MCPAuthASGI(inner)
    messages = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "path": "",
        "headers": [(b"authorization", f"Bearer {row.plaintext_api_key}".encode())],
        "client": ("127.0.0.1", 1234),
    }
    await middleware(scope, lambda: None, send)
    assert seen["path"] == "/"
    assert messages[0]["status"] == 200


def test_get_mcp_asgi_app_singleton():
    assert get_mcp_asgi_app() is get_mcp_asgi_app()
    assert get_mcp_starlette() is get_mcp_starlette()
