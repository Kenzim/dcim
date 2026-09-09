"""Shared fixtures for MCP unit tests."""

import pytest

from app.core.mcp_auth import McpAuthContext
from app.mcp.context import mcp_auth_ctx
from tests.conftest import TestingSessionLocal


@pytest.fixture
def mcp_sessionlocal(monkeypatch):
    """Point MCP runtime/auth at the in-memory SQLite sessionmaker."""
    monkeypatch.setattr("app.core.database.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.mcp.runtime.SessionLocal", TestingSessionLocal)
    return TestingSessionLocal


@pytest.fixture
def mcp_auth_ctx_read():
    ctx = McpAuthContext(
        key_id=1,
        name="test-read",
        scopes=["read"],
        created_by_user_id=1,
        client_ip="127.0.0.1",
    )
    token = mcp_auth_ctx.set(ctx)
    try:
        yield ctx
    finally:
        mcp_auth_ctx.reset(token)


@pytest.fixture
def mcp_auth_ctx_write():
    ctx = McpAuthContext(
        key_id=2,
        name="test-write",
        scopes=["write"],
        created_by_user_id=1,
        client_ip="127.0.0.1",
    )
    token = mcp_auth_ctx.set(ctx)
    try:
        yield ctx
    finally:
        mcp_auth_ctx.reset(token)


@pytest.fixture
def mcp_auth_ctx_destructive():
    ctx = McpAuthContext(
        key_id=3,
        name="test-destructive",
        scopes=["destructive"],
        created_by_user_id=1,
        client_ip="127.0.0.1",
    )
    token = mcp_auth_ctx.set(ctx)
    try:
        yield ctx
    finally:
        mcp_auth_ctx.reset(token)
