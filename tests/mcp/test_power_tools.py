"""MCP power tool read-path tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.models.location import Location
from app.models.server import Server
from app.plugins.base import PowerState


@pytest.fixture
def power_server(db_session):
    loc = Location(name="pwr", description="")
    db_session.add(loc)
    db_session.flush()
    server = Server(
        name="pwr-srv",
        server_ip="10.1.2.3",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


@pytest.mark.asyncio
async def test_get_server_power_state(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read, power_server, monkeypatch
):
    plugin = SimpleNamespace(get_power_state=AsyncMock(return_value=PowerState.ON))
    monkeypatch.setattr("app.mcp.tools.power._plugin_for_server", lambda db, s: plugin)
    monkeypatch.setattr("app.mcp.tools.power._server_has_capability", lambda db, s, c: True)
    from app.mcp.tools.power import get_server_power_state

    result = await get_server_power_state(power_server.id)
    assert result["power_state"] == "on"


@pytest.mark.asyncio
async def test_get_server_power_state_not_found(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    from app.mcp.tools.power import get_server_power_state

    with pytest.raises(ToolError, match="Server not found"):
        await get_server_power_state(999999)


@pytest.mark.asyncio
async def test_get_server_boot_options(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read, power_server, monkeypatch
):
    plugin = SimpleNamespace(
        get_boot_options=AsyncMock(return_value=["disk", "pxe"]),
        get_boot_order=AsyncMock(return_value={"device": "disk"}),
    )
    monkeypatch.setattr("app.mcp.tools.power._plugin_for_server", lambda db, s: plugin)
    monkeypatch.setattr("app.mcp.tools.power._server_has_capability", lambda db, s, c: True)
    from app.mcp.tools.power import get_server_boot_options

    result = await get_server_boot_options(power_server.id)
    assert result["options"] == ["disk", "pxe"]


@pytest.mark.asyncio
async def test_list_install_tasks_empty(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read, power_server
):
    from app.mcp.tools.power import list_install_tasks

    result = await list_install_tasks(power_server.id)
    assert result["tasks"] == []
