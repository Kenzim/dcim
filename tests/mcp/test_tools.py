"""Curated MCP tool happy paths (search, power confirm, provision, catalog)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.dao.permission_set_dao import PermissionSetDAO
from app.models.location import Location
from app.models.product_catalog import Product, ProductFamily
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
        "app.mcp.tools.power._plugin_for_server", lambda db, server: plugin
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


@pytest.mark.asyncio
async def test_rackflow_status_reports_key_and_tools(mcp_auth_ctx_read, mcp_sessionlocal):
    from app.mcp.tools.meta import rackflow_status

    result = await rackflow_status()
    assert result["product"] == "Rackflow"
    assert result["key_name"] == "test-read"
    assert result["scopes"] == ["read"]
    assert result["tool_count"] >= 1


@pytest.mark.asyncio
async def test_list_locations_and_create(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write
):
    from app.mcp.tools.inventory import create_location, list_locations

    created = await create_location("MCP-DC", description="test site")
    assert created["name"] == "MCP-DC"
    listed = await list_locations()
    names = [row["name"] for row in listed["locations"]]
    assert "MCP-DC" in names


@pytest.mark.asyncio
async def test_list_families_and_products(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    family = ProductFamily(
        name="VM Family",
        code="vm-fam",
        service_type="vm",
        enabled=True,
    )
    db_session.add(family)
    db_session.commit()
    db_session.refresh(family)
    product = Product(
        name="Small VM",
        code="vm-small",
        family_id=family.id,
        enabled=True,
    )
    db_session.add(product)
    db_session.commit()

    from app.mcp.tools.catalog import list_families, list_products

    families = await list_families()
    assert any(row["code"] == "vm-fam" for row in families["families"])
    products = await list_products()
    assert any(row["code"] == "vm-small" for row in products["products"])


@pytest.mark.asyncio
async def test_list_clients_and_create(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write
):
    preset = PermissionSetDAO.create(db_session, name="client-default", permissions={})
    from app.mcp.tools.users import create_client, get_client, list_clients

    created = await create_client(
        username="mcp-user",
        email="mcp@example.com",
        permission_set_id=preset.id,
    )
    assert created["username"] == "mcp-user"
    assert created["permission_set_id"] == preset.id

    listed = await list_clients(query="mcp-user")
    assert any(row["username"] == "mcp-user" for row in listed["clients"])

    fetched = await get_client(created["id"])
    assert fetched["email"] == "mcp@example.com"


@pytest.mark.asyncio
async def test_list_virtual_media_profiles(mcp_auth_ctx_read, mcp_sessionlocal):
    from app.mcp.tools.console import list_virtual_media_profiles

    result = await list_virtual_media_profiles()
    ids = {row["id"] for row in result["profiles"]}
    assert {"asrockrack", "gigabyte", "supermicro"} <= ids


@pytest.mark.asyncio
async def test_insert_virtual_media_requires_confirm(mcp_auth_ctx_destructive, mcp_sessionlocal):
    from app.mcp.tools.console import insert_virtual_media, eject_virtual_media

    with pytest.raises(ToolError, match="confirm=true"):
        await insert_virtual_media(filename="ubuntu.iso", server_id=1, confirm=False)
    with pytest.raises(ToolError, match="confirm=true"):
        await eject_virtual_media(server_id=1, confirm=False)


@pytest.mark.asyncio
async def test_list_subnets_empty(db_session, mcp_auth_ctx_read, mcp_sessionlocal):
    from app.mcp.tools.ipam import list_subnets

    result = await list_subnets()
    assert result["subnets"] == []
