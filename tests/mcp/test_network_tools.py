"""MCP network tool read-path tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.models.location import Location
from app.models.network_switch import NetworkSwitch


@pytest.mark.asyncio
async def test_list_switches_all_and_by_location(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    loc1 = Location(name="net-l1", description="")
    loc2 = Location(name="net-l2", description="")
    db_session.add_all([loc1, loc2])
    db_session.flush()
    sw1 = NetworkSwitch(
        name="sw-a",
        location_id=loc1.id,
        plugin_name="snmp",
        plugin_config={},
        enabled=True,
    )
    sw2 = NetworkSwitch(
        name="sw-b",
        location_id=loc2.id,
        plugin_name="snmp",
        plugin_config={},
        enabled=True,
    )
    db_session.add_all([sw1, sw2])
    db_session.commit()

    from app.mcp.tools.network import list_switches

    all_rows = await list_switches()
    names = {row["name"] for row in all_rows["switches"]}
    assert {"sw-a", "sw-b"} <= names

    filtered = await list_switches(location_id=loc1.id)
    assert [row["name"] for row in filtered["switches"]] == ["sw-a"]


@pytest.mark.asyncio
async def test_list_switch_ports_and_not_found(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    from app.models.switch_port import SwitchPort

    loc = Location(name="port-loc", description="")
    db_session.add(loc)
    db_session.flush()
    sw = NetworkSwitch(
        name="sw-ports",
        location_id=loc.id,
        plugin_name="snmp",
        plugin_config={},
        enabled=True,
    )
    db_session.add(sw)
    db_session.flush()
    db_session.add(
        SwitchPort(switch_id=sw.id, name="Gi0/1", if_index=1, speed_mbps=1000)
    )
    db_session.commit()
    db_session.refresh(sw)

    from app.mcp.tools.network import list_switch_ports

    result = await list_switch_ports(sw.id)
    assert result["ports"][0]["name"] == "Gi0/1"

    with pytest.raises(ToolError, match="Switch not found"):
        await list_switch_ports(999999)


def _snmp_config(**overrides):
    config = {
        "hostname": "10.0.0.9",
        "username": "rfmonitor",
        "security_level": "authPriv",
        "auth_protocol": "SHA",
        "auth_password": "auth-secret",
        "priv_protocol": "AES",
        "priv_password": "priv-secret",
    }
    config.update(overrides)
    return config


@pytest.fixture
def no_port_discovery(monkeypatch):
    monkeypatch.setattr("app.api.network_switch.switch_plugin_supports", lambda name, cap: False)


@pytest.mark.asyncio
async def test_list_switch_plugins(mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.network import list_switch_plugins

    plugins = {p["name"]: p for p in (await list_switch_plugins())["plugins"]}
    assert "snmpv3" in plugins
    assert "monitoring" in plugins["snmpv3"]["capabilities"]
    assert "hostname" in plugins["snmpv3"]["required"]
    assert "auth_password" in plugins["snmpv3"]["fields"]


@pytest.mark.asyncio
async def test_create_get_update_switch_masks_and_merges_secrets(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write, no_port_discovery
):
    loc = Location(name="sw-create-loc", description="")
    db_session.add(loc)
    db_session.commit()

    from app.mcp.tools.network import create_switch, get_switch, update_switch

    created = await create_switch(
        name="core-1", location_id=loc.id, plugin_name="snmpv3", plugin_config=_snmp_config(), model="N3K"
    )
    assert created["plugin_config"]["hostname"] == "10.0.0.9"
    assert created["plugin_config"]["auth_password"] == "***"
    assert created["plugin_config"]["priv_password"] == "***"

    fetched = await get_switch(created["id"])
    assert fetched["model"] == "N3K"
    assert fetched["plugin_config"]["priv_password"] == "***"

    await update_switch(created["id"], plugin_config={"hostname": "10.0.0.10", "auth_password": "***"})
    db_session.expire_all()
    row = db_session.get(NetworkSwitch, created["id"])
    assert row.plugin_config["hostname"] == "10.0.0.10"
    assert row.plugin_config["auth_password"] == "auth-secret"
    assert row.plugin_config["priv_password"] == "priv-secret"

    with pytest.raises(ToolError, match="already exists"):
        await create_switch(name="core-1", location_id=loc.id, plugin_name="snmpv3", plugin_config=_snmp_config())
    with pytest.raises(ToolError, match="not found in registry"):
        await create_switch(name="core-2", location_id=loc.id, plugin_name="nope", plugin_config={})


@pytest.mark.asyncio
async def test_create_switch_needs_write_scope(mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.network import create_switch

    with pytest.raises(ToolError, match="scope"):
        await create_switch(name="x", location_id=1, plugin_name="snmpv3", plugin_config={})


@pytest.mark.asyncio
async def test_delete_switch_requires_confirm(db_session, mcp_sessionlocal, mcp_auth_ctx_destructive):
    loc = Location(name="sw-del-loc", description="")
    db_session.add(loc)
    db_session.flush()
    sw = NetworkSwitch(name="sw-del", location_id=loc.id, plugin_name="snmpv3", plugin_config={}, enabled=True)
    db_session.add(sw)
    db_session.commit()
    switch_id = sw.id

    from app.mcp.tools.network import delete_switch

    with pytest.raises(ToolError, match="confirm"):
        await delete_switch(switch_id)
    assert (await delete_switch(switch_id, confirm=True))["deleted"] is True
    db_session.expunge_all()
    assert db_session.query(NetworkSwitch).filter_by(id=switch_id).count() == 0


@pytest.mark.asyncio
async def test_regenerate_ports_recreates_uncabled_ports(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write, monkeypatch
):
    from app.models.switch_port import SwitchPort

    loc = Location(name="sw-regen-loc", description="")
    db_session.add(loc)
    db_session.flush()
    sw = NetworkSwitch(name="sw-regen", location_id=loc.id, plugin_name="snmpv3", plugin_config={}, enabled=True)
    db_session.add(sw)
    db_session.flush()
    db_session.add(SwitchPort(switch_id=sw.id, name="Ethernet1/1", if_index=1, speed_mbps=10000))
    db_session.commit()

    class FakePlugin:
        async def get_all_port_statistics(self):
            return {
                "Ethernet1/1": {"ifType": 6, "ifIndex": 1, "ifHighSpeed": 10000},
                "Ethernet1/2": {"ifType": 6, "ifIndex": 2, "ifHighSpeed": 10000},
                "Vlan10": {"ifType": 136, "ifIndex": 50},
            }

    registry = MagicMock()
    registry.get_plugin.return_value = FakePlugin()
    monkeypatch.setattr("app.api.network_switch.get_switch_registry", lambda: registry)

    from app.mcp.tools.network import regenerate_switch_ports

    result = await regenerate_switch_ports(sw.id)
    assert result["deleted"] == 1 and result["created"] == 2
    db_session.expire_all()
    names = sorted(p.name for p in db_session.query(SwitchPort).filter_by(switch_id=sw.id))
    assert names == ["Ethernet1/1", "Ethernet1/2"]


@pytest.mark.asyncio
async def test_get_aggregate_bandwidth(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read, monkeypatch
):
    monkeypatch.setattr(
        "app.mcp.tools.network.get_aggregate_monitored_bandwidth",
        lambda db, hours=24: {"hours": hours, "ports": [], "totals": {"in_bps": 0, "out_bps": 0}},
    )
    from app.mcp.tools.network import get_aggregate_bandwidth

    result = await get_aggregate_bandwidth(hours=12)
    assert result["hours"] == 12


@pytest.mark.asyncio
async def test_moving_switch_from_snmpv3_to_nxos_keeps_ports_cables_and_snmp_login(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write
):
    from app.models.cable_run import CableRun
    from app.models.network_port import NetworkPort
    from app.models.server import Server
    from app.models.switch_port import SwitchPort

    loc = Location(name="sw-move-loc", description="")
    db_session.add(loc)
    db_session.flush()
    sw = NetworkSwitch(name="sw-move", location_id=loc.id, plugin_name="snmpv3", plugin_config=_snmp_config(),
                       enabled=True)
    server = Server(name="srv-move", server_ip="10.1.1.1", location_id=loc.id, plugin_name="ipmi", plugin_config={})
    db_session.add_all([sw, server])
    db_session.flush()
    port = SwitchPort(switch_id=sw.id, name="Ethernet1/1", if_index=1, speed_mbps=10000)
    nic = NetworkPort(server_id=server.id, name="eth0", speed_mbps=10000)
    db_session.add_all([port, nic])
    db_session.flush()
    cable = CableRun(end_a_server_port_id=nic.id, end_b_switch_port_id=port.id)
    db_session.add(cable)
    db_session.commit()
    port_id, cable_id, switch_id = port.id, cable.id, sw.id

    from app.mcp.tools.network import update_switch

    with pytest.raises(ToolError, match="incomplete"):
        await update_switch(switch_id, plugin_name="nxos")

    await update_switch(switch_id, plugin_name="nxos", plugin_config={"nxapi_username": "rackflow",
                                                                      "nxapi_password": "nx-secret"})
    db_session.expire_all()
    row = db_session.get(NetworkSwitch, switch_id)
    assert row.plugin_name == "nxos"
    assert row.plugin_config["auth_password"] == "auth-secret" and row.plugin_config["username"] == "rfmonitor"
    assert row.plugin_config["nxapi_password"] == "nx-secret"
    assert db_session.get(SwitchPort, port_id) is not None
    assert db_session.get(CableRun, cable_id).end_b_switch_port_id == port_id


@pytest.mark.asyncio
async def test_switch_rows_show_vpc_pairing_and_detect_runs(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write, monkeypatch
):
    loc = Location(name="sw-vpc-loc", description="")
    db_session.add(loc)
    db_session.flush()
    a = NetworkSwitch(name="vpc-a", location_id=loc.id, plugin_name="snmpv3", plugin_config={}, enabled=True)
    b = NetworkSwitch(name="vpc-b", location_id=loc.id, plugin_name="snmpv3", plugin_config={}, enabled=True)
    db_session.add_all([a, b])
    db_session.flush()
    a.vpc_domain_id, a.vpc_peer_switch_id, a.vpc_peer_link = 1, b.id, "port-channel45"
    db_session.commit()

    from app.mcp.tools.network import detect_switch_vpc, get_switch

    row = await get_switch(a.id)
    assert row["vpc"]["domain_id"] == 1 and row["vpc"]["peer_switch_id"] == b.id
    assert row["vpc"]["peer_link"] == "port-channel45"

    async def fake_detect(db):
        return {"switches": [], "pairs": [(a.id, b.id)]}

    monkeypatch.setattr("app.services.switch_vpc_service.detect_vpc_pairs", fake_detect)
    result = await detect_switch_vpc()
    assert [list(p) for p in result["pairs"]] == [[a.id, b.id]]
