"""Curated MCP tool happy paths (search, power confirm, provision)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.models.location import Location
from app.models.server import Server
from app.models.service import ServiceStatus
from app.models.user import User


@pytest.mark.asyncio
async def test_search_finds_server_and_client(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    loc = Location(name="Lon", description="")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)
    server = Server(
        name="web-01",
        server_ip="10.9.8.7",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    client = User(username="alice", email="alice@example.com", is_admin=False)
    client.set_password(None)
    db_session.add_all([server, client])
    db_session.commit()

    from app.mcp.tools.meta import search

    result = await search("web")
    names = [row["name"] for row in result["servers"]]
    assert "web-01" in names
    by_ip = await search("10.9.8.7")
    assert any(row["server_ip"] == "10.9.8.7" for row in by_ip["servers"])
    people = await search("alice")
    assert any(row["username"] == "alice" for row in people["clients"])


@pytest.mark.asyncio
async def test_server_power_off_requires_confirm(
    mcp_auth_ctx_destructive, mcp_sessionlocal, db_session
):
    from app.mcp.tools.power import server_power

    with pytest.raises(ToolError, match="confirm=true"):
        await server_power(1, "off", confirm=False)


@pytest.mark.asyncio
async def test_server_power_on(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write, monkeypatch
):
    loc = Location(name="NYC", description="")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)
    server = Server(
        name="bmc-1",
        server_ip="10.0.0.9",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    plugin = SimpleNamespace()
    plugin.power_on = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.mcp.tools.power._plugin_for_server", AsyncMock(return_value=plugin)
    )
    monkeypatch.setattr(
        "app.mcp.tools.power._server_has_capability", lambda db, s, cap: True
    )

    from app.mcp.tools.power import server_power

    result = await server_power(server.id, "on")
    assert result["success"] is True
    assert result["action"] == "on"
    plugin.power_on.assert_awaited()


@pytest.mark.asyncio
async def test_provision_vm_calls_core(
    mcp_auth_ctx_write, mcp_sessionlocal, db_session, monkeypatch
):
    fake = SimpleNamespace(id=42, status=ServiceStatus.PENDING)
    monkeypatch.setattr(
        "app.mcp.tools.services._create_admin_vm_core", lambda db, body: fake
    )
    called = {}

    def _enqueue(db, service_id):
        called["id"] = service_id
        return fake, None

    monkeypatch.setattr(
        "app.mcp.tools.services.provision_vm_service_async", _enqueue
    )
    monkeypatch.setattr(
        "app.mcp.tools.services.service_row",
        lambda db, row: {"id": row.id, "name": "vm-a"},
    )

    from app.mcp.tools.services import provision_vm

    result = await provision_vm(
        name="vm-a", product_code="sku", vm_template_id=1, auto_provision=True
    )
    assert result["id"] == 42
    assert called["id"] == 42
