"""MCP inventory tool tests with mocked DAOs where needed."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.models.location import Location
from app.models.server import Server


def _loc(id=1, name="DC1", description="site"):
    return SimpleNamespace(id=id, name=name, description=description)


def _server(id=1, name="srv-1", server_ip="10.0.0.1", location_id=1, enabled=True, **kwargs):
    defaults = {
        "id": id,
        "name": name,
        "server_ip": server_ip,
        "location_id": location_id,
        "description": None,
        "rack_id": None,
        "rack_unit": None,
        "rack_units": None,
        "plugin_name": "ipmi",
        "enabled": enabled,
        "cpu_count": 1,
        "cpu_model": None,
        "ram_gb": None,
        "ipmi_proxy_enabled": False,
        "plugin_config": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_list_locations_returns_rows(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    loc = Location(name="inv-dc", description="test")
    db_session.add(loc)
    db_session.commit()

    from app.mcp.tools.inventory import list_locations

    result = await list_locations()
    assert any(row["name"] == "inv-dc" for row in result["locations"])


@pytest.mark.asyncio
async def test_get_location_success(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    loc = Location(name="get-loc", description="desc")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)

    from app.mcp.tools.inventory import get_location

    result = await get_location(loc.id)
    assert result["id"] == loc.id
    assert result["name"] == "get-loc"
    assert result["description"] == "desc"


@pytest.mark.asyncio
async def test_get_location_not_found(db_session, mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.inventory import get_location

    with pytest.raises(ToolError, match="Location not found"):
        await get_location(99999)


@pytest.mark.asyncio
async def test_list_servers_all_and_filtered(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    loc1 = Location(name="L1", description="")
    loc2 = Location(name="L2", description="")
    db_session.add_all([loc1, loc2])
    db_session.commit()
    db_session.refresh(loc1)
    db_session.refresh(loc2)

    s1 = Server(
        name="on-1",
        server_ip="10.0.0.1",
        location_id=loc1.id,
        plugin_name="ipmi",
        plugin_config={},
        enabled=True,
    )
    s2 = Server(
        name="off-1",
        server_ip="10.0.0.2",
        location_id=loc1.id,
        plugin_name="ipmi",
        plugin_config={},
        enabled=False,
    )
    s3 = Server(
        name="other",
        server_ip="10.0.0.3",
        location_id=loc2.id,
        plugin_name="ipmi",
        plugin_config={},
        enabled=True,
    )
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    from app.mcp.tools.inventory import list_servers

    all_servers = await list_servers()
    names = {row["name"] for row in all_servers["servers"]}
    assert {"on-1", "off-1", "other"} <= names

    by_loc = await list_servers(location_id=loc1.id, enabled_only=True)
    assert [row["name"] for row in by_loc["servers"]] == ["on-1"]


@pytest.mark.asyncio
async def test_list_servers_respects_limit(
    mcp_sessionlocal, mcp_auth_ctx_read, monkeypatch
):
    servers = [_server(id=i, name=f"s{i}", server_ip=f"10.0.0.{i}") for i in range(1, 6)]
    monkeypatch.setattr(
        "app.mcp.tools.inventory.ServerDAO.get_all",
        lambda db, skip=0, limit=100, enabled_only=False: servers,
    )

    from app.mcp.tools.inventory import list_servers

    result = await list_servers(limit=3)
    assert len(result["servers"]) == 3


@pytest.mark.asyncio
async def test_get_server_includes_capabilities(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read, monkeypatch
):
    loc = Location(name="Cap", description="")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)
    server = Server(
        name="cap-srv",
        server_ip="10.1.1.1",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    monkeypatch.setattr(
        "app.mcp.tools.inventory._get_effective_capabilities_for_server",
        lambda db, row: [{"name": "kvm", "enabled": True}],
    )

    from app.mcp.tools.inventory import get_server

    result = await get_server(server.id)
    assert result["name"] == "cap-srv"
    assert result["capabilities"] == [{"name": "kvm", "enabled": True}]


@pytest.mark.asyncio
async def test_get_server_not_found(db_session, mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.inventory import get_server

    with pytest.raises(ToolError, match="Server not found"):
        await get_server(12345)


@pytest.mark.asyncio
async def test_create_server_location_not_found(
    mcp_sessionlocal, mcp_auth_ctx_write, monkeypatch
):
    monkeypatch.setattr(
        "app.mcp.tools.inventory.LocationDAO.get_by_id",
        lambda db, loc_id: None,
    )

    from app.mcp.tools.inventory import create_server

    with pytest.raises(ToolError, match="Location not found"):
        await create_server(
            name="new",
            server_ip="10.0.0.5",
            plugin_name="ipmi",
            location_id=99,
        )


@pytest.mark.asyncio
async def test_create_server_duplicate_name(
    mcp_sessionlocal, mcp_auth_ctx_write, monkeypatch
):
    monkeypatch.setattr(
        "app.mcp.tools.inventory.LocationDAO.get_by_id",
        lambda db, loc_id: _loc(loc_id),
    )
    monkeypatch.setattr(
        "app.mcp.tools.inventory.ServerDAO.get_by_name",
        lambda db, name: _server(name=name),
    )

    from app.mcp.tools.inventory import create_server

    with pytest.raises(ToolError, match="already exists"):
        await create_server(
            name="dup",
            server_ip="10.0.0.5",
            plugin_name="ipmi",
            location_id=1,
        )


@pytest.mark.asyncio
async def test_create_server_unknown_plugin(
    mcp_sessionlocal, mcp_auth_ctx_write, monkeypatch
):
    monkeypatch.setattr(
        "app.mcp.tools.inventory.LocationDAO.get_by_id",
        lambda db, loc_id: _loc(loc_id),
    )
    monkeypatch.setattr(
        "app.mcp.tools.inventory.ServerDAO.get_by_name",
        lambda db, name: None,
    )
    registry = MagicMock()
    registry.get_plugin_class.return_value = None
    monkeypatch.setattr(
        "app.mcp.tools.inventory.get_registry",
        lambda: registry,
    )

    from app.mcp.tools.inventory import create_server

    with pytest.raises(ToolError, match="Unknown plugin"):
        await create_server(
            name="new",
            server_ip="10.0.0.5",
            plugin_name="bogus",
            location_id=1,
        )


@pytest.mark.asyncio
async def test_create_server_success(
    mcp_sessionlocal, mcp_auth_ctx_write, monkeypatch
):
    created = _server(id=7, name="new-srv", server_ip="10.0.0.7")
    monkeypatch.setattr(
        "app.mcp.tools.inventory.LocationDAO.get_by_id",
        lambda db, loc_id: _loc(loc_id),
    )
    monkeypatch.setattr(
        "app.mcp.tools.inventory.ServerDAO.get_by_name",
        lambda db, name: None,
    )
    monkeypatch.setattr(
        "app.mcp.tools.inventory.ServerDAO.create",
        lambda db, **kwargs: created,
    )
    registry = MagicMock()
    registry.get_plugin_class.return_value = object
    monkeypatch.setattr(
        "app.mcp.tools.inventory.get_registry",
        lambda: registry,
    )

    from app.mcp.tools.inventory import create_server

    result = await create_server(
        name="new-srv",
        server_ip="10.0.0.7",
        plugin_name="ipmi",
        location_id=1,
        description="test box",
    )
    assert result["id"] == 7
    assert result["name"] == "new-srv"
    assert "plugin_config" not in result


@pytest.mark.asyncio
async def test_update_server_not_found(
    mcp_sessionlocal, mcp_auth_ctx_write, monkeypatch
):
    monkeypatch.setattr(
        "app.mcp.tools.inventory.ServerDAO.get_by_id",
        lambda db, sid: None,
    )

    from app.mcp.tools.inventory import update_server

    with pytest.raises(ToolError, match="Server not found"):
        await update_server(1, name="x")


@pytest.mark.asyncio
async def test_update_server_location_not_found(
    mcp_sessionlocal, mcp_auth_ctx_write, monkeypatch
):
    row = _server()
    monkeypatch.setattr(
        "app.mcp.tools.inventory.ServerDAO.get_by_id",
        lambda db, sid: row,
    )
    monkeypatch.setattr(
        "app.mcp.tools.inventory.LocationDAO.get_by_id",
        lambda db, loc_id: None,
    )

    from app.mcp.tools.inventory import update_server

    with pytest.raises(ToolError, match="Location not found"):
        await update_server(1, location_id=99)


@pytest.mark.asyncio
async def test_update_server_success(
    mcp_sessionlocal, mcp_auth_ctx_write, monkeypatch
):
    row = _server(name="old", enabled=True)
    updated = _server(name="renamed", enabled=False)

    monkeypatch.setattr(
        "app.mcp.tools.inventory.ServerDAO.get_by_id",
        lambda db, sid: row,
    )
    monkeypatch.setattr(
        "app.mcp.tools.inventory.ServerDAO.update",
        lambda db, r: updated,
    )

    from app.mcp.tools.inventory import update_server

    result = await update_server(1, name="renamed", enabled=False)
    assert result["name"] == "renamed"
    assert result["enabled"] is False
    assert row.name == "renamed"
    assert row.enabled is False


@pytest.mark.asyncio
async def test_create_and_update_location(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write
):
    from app.mcp.tools.inventory import create_location, update_location

    created = await create_location("new-dc", description="site")
    assert created["name"] == "new-dc"
    updated = await update_location(created["id"], name="renamed-dc")
    assert updated["name"] == "renamed-dc"


@pytest.mark.asyncio
async def test_update_location_not_found(db_session, mcp_sessionlocal, mcp_auth_ctx_write):
    from app.mcp.tools.inventory import update_location

    with pytest.raises(ToolError, match="Location not found"):
        await update_location(99999, name="x")


@pytest.mark.asyncio
async def test_rack_crud_flow(db_session, mcp_sessionlocal, mcp_auth_ctx_write):
    from app.models.rack import Rack
    from app.mcp.tools.inventory import create_rack, list_racks, update_rack

    loc = Location(name="rack-loc", description="")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)

    created = await create_rack(loc.id, name="R-A", units=48, description="row 1")
    assert created["name"] == "R-A"
    assert created["units"] == 48

    listed = await list_racks(location_id=loc.id)
    assert any(r["name"] == "R-A" for r in listed["racks"])

    updated = await update_rack(created["id"], name="R-B", units=42)
    assert updated["name"] == "R-B"
    assert updated["units"] == 42


@pytest.mark.asyncio
async def test_create_rack_duplicate_name(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write
):
    from app.models.rack import Rack
    from app.mcp.tools.inventory import create_rack

    loc = Location(name="dup-rack-loc", description="")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)
    db_session.add(Rack(location_id=loc.id, name="R1", units=42))
    db_session.commit()

    with pytest.raises(ToolError, match="already exists"):
        await create_rack(loc.id, name="R1")


@pytest.mark.asyncio
async def test_delete_server_requires_confirm_and_success(
    db_session, mcp_sessionlocal, mcp_auth_ctx_destructive
):
    from app.mcp.tools.inventory import delete_server

    loc = Location(name="del-loc", description="")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)
    server = Server(
        name="del-me",
        server_ip="10.9.9.9",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    with pytest.raises(ToolError, match="confirm"):
        await delete_server(server.id, confirm=False)

    result = await delete_server(server.id, confirm=True)
    assert result["deleted"] is True
    assert result["server_id"] == server.id


@pytest.mark.asyncio
async def test_list_server_groups_and_activity(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    from app.mcp.tools.inventory import get_server_activity, list_server_groups

    groups = await list_server_groups()
    assert "groups" in groups

    loc = Location(name="act-loc", description="")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)
    server = Server(
        name="act-srv",
        server_ip="10.8.8.8",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    activity = await get_server_activity(server.id, limit=5)
    assert activity["activity"] == []


@pytest.mark.asyncio
async def test_get_server_bandwidth_empty_ports(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    from app.mcp.tools.inventory import get_server_bandwidth

    loc = Location(name="bw-loc", description="")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)
    server = Server(
        name="bw-srv",
        server_ip="10.7.7.7",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    result = await get_server_bandwidth(server.id)
    assert result["server_id"] == server.id
    assert result["ports"] == []
