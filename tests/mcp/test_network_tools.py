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
