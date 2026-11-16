"""MCP cabling tools: server NICs and cable runs."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.models.cable_run import CableRun
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.server import Server
from app.models.switch_port import SwitchPort


@pytest.fixture
def rig(db_session):
    loc = Location(name="cab-loc", description="")
    db_session.add(loc)
    db_session.flush()
    server = Server(name="cab-srv", server_ip="10.9.9.9", location_id=loc.id, plugin_name="ipmi", plugin_config={})
    switch = NetworkSwitch(name="cab-sw", location_id=loc.id, plugin_name="snmpv3", plugin_config={}, enabled=True)
    db_session.add_all([server, switch])
    db_session.flush()
    db_session.add_all(
        [
            SwitchPort(switch_id=switch.id, name="Ethernet1/1", if_index=1, speed_mbps=10000),
            SwitchPort(switch_id=switch.id, name="Ethernet1/2", if_index=2, speed_mbps=10000),
        ]
    )
    db_session.commit()
    return {"server": server.id, "switch": switch.id}


@pytest.mark.asyncio
async def test_save_server_ports_upserts_without_deleting(db_session, mcp_sessionlocal, mcp_auth_ctx_write, rig):
    from app.mcp.tools.cabling import save_server_ports

    sid = rig["server"]
    first = await save_server_ports(sid, [{"name": "eno1", "speed_mbps": 10000, "pxe_boot": True}])
    assert first["created"] == ["eno1"]
    second = await save_server_ports(
        sid, [{"name": "eno1", "mac_address": "aa:bb:cc:dd:ee:01"}, {"name": "eno2", "speed_mbps": 1000}]
    )
    assert second["created"] == ["eno2"] and second["updated"] == ["eno1"]
    db_session.expire_all()
    rows = {p.name: p for p in db_session.query(NetworkPort).filter_by(server_id=sid)}
    assert set(rows) == {"eno1", "eno2"}
    assert rows["eno1"].mac_address == "aa:bb:cc:dd:ee:01" and rows["eno1"].pxe_boot is True

    with pytest.raises(ToolError, match="needs speed_mbps"):
        await save_server_ports(sid, [{"name": "eno3"}])
    with pytest.raises(ToolError, match="Only one PXE"):
        await save_server_ports(sid, [{"name": "eno2", "pxe_boot": True}])
    with pytest.raises(ToolError, match="Unknown port fields"):
        await save_server_ports(sid, [{"name": "eno1", "vlan": 5}])


@pytest.mark.asyncio
async def test_cable_connect_list_disconnect_by_name(db_session, mcp_sessionlocal, mcp_auth_ctx_write, rig):
    from app.mcp.tools.cabling import cable, save_server_ports

    sid, swid = rig["server"], rig["switch"]
    await save_server_ports(sid, [{"name": "eno1", "speed_mbps": 10000}, {"name": "eno2", "speed_mbps": 10000}])

    made = await cable(
        "connect",
        runs=[
            {"a": f"server:{sid}/eno1", "b": f"switch:{swid}/Ethernet1/1", "cable_type": "DAC"},
            {"a": f"server:{sid}/eno2", "b": f"switch:{swid}/Ethernet1/2"},
        ],
    )
    assert len(made["created"]) == 2
    assert made["created"][0]["b"]["port"] == "Ethernet1/1"
    assert made["created"][0]["cable_type"] == "DAC"

    listed = await cable("list", server_id=sid)
    assert len(listed["cables"]) == 2

    from app.mcp.tools.combined import server_info

    ports = (await server_info(sid, view="ports"))["ports"]
    assert {p["name"]: p["cable_run"]["other_end_port_name"] for p in ports} == {
        "eno1": "Ethernet1/1",
        "eno2": "Ethernet1/2",
    }

    with pytest.raises(ToolError, match="already cabled"):
        await cable("connect", a=f"server:{sid}/eno1", b=f"switch:{swid}/Ethernet1/2")

    gone = await cable("disconnect", a=f"switch:{swid}/Ethernet1/1")
    assert gone["deleted"]["a"]["port"] == "eno1"
    db_session.expire_all()
    assert db_session.query(CableRun).count() == 1


@pytest.mark.asyncio
async def test_cable_connect_is_all_or_nothing(db_session, mcp_sessionlocal, mcp_auth_ctx_write, rig):
    from app.mcp.tools.cabling import cable, save_server_ports

    sid, swid = rig["server"], rig["switch"]
    await save_server_ports(sid, [{"name": "eno1", "speed_mbps": 10000}])
    with pytest.raises(ToolError, match="Run 2: No port named"):
        await cable(
            "connect",
            runs=[
                {"a": f"server:{sid}/eno1", "b": f"switch:{swid}/Ethernet1/1"},
                {"a": f"server:{sid}/missing", "b": f"switch:{swid}/Ethernet1/2"},
            ],
        )
    with pytest.raises(ToolError, match="used twice"):
        await cable(
            "connect",
            runs=[
                {"a": f"server:{sid}/eno1", "b": f"switch:{swid}/Ethernet1/1"},
                {"a": f"server:{sid}/eno1", "b": f"switch:{swid}/Ethernet1/2"},
            ],
        )
    with pytest.raises(ToolError, match="Bad cable end"):
        await cable("connect", a="rack:1", b=f"switch:{swid}/Ethernet1/1")
    db_session.expire_all()
    assert db_session.query(CableRun).count() == 0


@pytest.mark.asyncio
async def test_cable_update_and_scopes(db_session, mcp_sessionlocal, mcp_auth_ctx_write, rig):
    from app.mcp.tools.cabling import cable, save_server_ports

    sid, swid = rig["server"], rig["switch"]
    await save_server_ports(sid, [{"name": "eno1", "speed_mbps": 10000}])
    made = await cable("connect", a=f"server:{sid}/eno1", b=f"switch:{swid}/Ethernet1/1")
    cid = made["created"][0]["id"]
    updated = await cable("update", cable_id=cid, description="patch panel 3", speed_mbps=10000)
    assert updated["description"] == "patch panel 3"
    with pytest.raises(ToolError, match="exactly one"):
        await cable("disconnect")


@pytest.mark.asyncio
async def test_cabling_write_needs_write_scope(mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.cabling import cable

    with pytest.raises(ToolError, match="scope"):
        await cable("connect", a="server:1", b="switch:1")


@pytest.mark.asyncio
async def test_delete_server_port_requires_confirm(db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, rig):
    from app.mcp.tools.cabling import delete_server_port, save_server_ports

    sid = rig["server"]
    port_id = (await save_server_ports(sid, [{"name": "eno1", "speed_mbps": 1000}]))["ports"][0]["id"]
    with pytest.raises(ToolError, match="confirm"):
        await delete_server_port(sid, port_id)
    assert (await delete_server_port(sid, port_id, confirm=True))["deleted"] is True
