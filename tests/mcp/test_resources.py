"""MCP read-only resources (server, service, location)."""

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.models.location import Location
from app.models.server import Server
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User


@pytest.mark.asyncio
async def test_server_resource_returns_json(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read, monkeypatch
):
    loc = Location(name="DC1", description="")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)
    server = Server(
        name="srv-1",
        server_ip="10.0.0.1",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    monkeypatch.setattr(
        "app.mcp.resources._get_effective_capabilities_for_server",
        lambda db, row: [{"name": "power_control", "enabled": True}],
    )

    from app.mcp.resources import server_resource

    raw = await server_resource(str(server.id))
    payload = json.loads(raw)
    assert payload["id"] == server.id
    assert payload["name"] == "srv-1"
    assert payload["capabilities"] == [{"name": "power_control", "enabled": True}]


@pytest.mark.asyncio
async def test_server_resource_rejects_bad_id(mcp_auth_ctx_read, mcp_sessionlocal):
    from app.mcp.resources import server_resource

    with pytest.raises(ToolError, match="integer"):
        await server_resource("not-a-number")


@pytest.mark.asyncio
async def test_server_resource_not_found(db_session, mcp_auth_ctx_read, mcp_sessionlocal):
    from app.mcp.resources import server_resource

    with pytest.raises(ToolError, match="not found"):
        await server_resource("99999")


@pytest.mark.asyncio
async def test_service_resource_returns_json(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    owner = User(username="bob", email="bob@example.com", is_admin=False)
    owner.set_password(None)
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    service = Service(
        name="svc-a",
        owner_user_id=owner.id,
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.ACTIVE,
    )
    db_session.add(service)
    db_session.commit()
    db_session.refresh(service)

    from app.mcp.resources import service_resource

    raw = await service_resource(str(service.id))
    payload = json.loads(raw)
    assert payload["id"] == service.id
    assert payload["name"] == "svc-a"
    assert payload["owner_username"] == "bob"


@pytest.mark.asyncio
async def test_location_resource_returns_json(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    loc = Location(name="Edge", description="edge site")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)

    from app.mcp.resources import location_resource

    raw = await location_resource(str(loc.id))
    payload = json.loads(raw)
    assert payload["id"] == loc.id
    assert payload["name"] == "Edge"
    assert payload["description"] == "edge site"
