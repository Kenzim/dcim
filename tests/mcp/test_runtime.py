"""Scope, confirm, redaction, and audit for MCP tools."""

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.mcp.runtime import redact, run_tool


def test_redact_secrets():
    payload = {
        "name": "box",
        "plugin_config": {"password": "secret"},
        "nested": {"api_key": "rfmcp_xxx", "ok": 1},
    }
    out = redact(payload)
    assert out["name"] == "box"
    assert out["plugin_config"] == "***"
    assert out["nested"]["api_key"] == "***"
    assert out["nested"]["ok"] == 1


@pytest.mark.asyncio
async def test_read_scope_denied(mcp_auth_ctx_read, mcp_sessionlocal, db_session):
    async def work(db, ctx):
        return {"ok": True}

    with pytest.raises(ToolError, match="lacks required scope"):
        await run_tool("create_location", "write", work)


@pytest.mark.asyncio
async def test_destructive_requires_confirm(
    mcp_auth_ctx_destructive, mcp_sessionlocal, db_session
):
    async def work(db, ctx):
        return {"deleted": True}

    with pytest.raises(ToolError, match="confirm=true"):
        await run_tool(
            "delete_server",
            "destructive",
            work,
            destructive=True,
            confirm=False,
            args={"server_id": 1},
        )


@pytest.mark.asyncio
async def test_run_tool_audits_success(
    mcp_auth_ctx_write, mcp_sessionlocal, db_session
):
    async def work(db, ctx):
        return {"name": "dc1"}

    result = await run_tool(
        "create_location",
        "write",
        work,
        args={"name": "dc1", "password": "should-redact"},
    )
    assert result == {"name": "dc1"}
    assert redact({"password": "should-redact"}) == {"password": "***"}
