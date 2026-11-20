"""Client and staff MCP for private network DHCP: owner sub-actions, scopes, views and the staff lookup."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.mcp.client.networks import NETWORK_SCOPE
from tests.api.test_private_network_sharing_api import _user, _vm, proxmox, world  # noqa: F401  (fixtures)
from tests.mcp.test_client_mcp import as_client, cmcp_db, rate_limit_redis  # noqa: F401  (fixtures)
from tests.mcp.test_private_network_sharing_mcp import _act, _joined, _reset_context, _view  # noqa: F401


def test_dhcp_sub_action_scopes():
    assert {k: v for k, v in NETWORK_SCOPE.items() if k.startswith("dhcp_")} == {
        "dhcp_get": "read",
        "dhcp_set": "write",
        "dhcp_reservation_add": "write",
        "dhcp_reservation_update": "write",
        "dhcp_reservation_remove": "destructive",
    }


@pytest.mark.asyncio
async def test_owner_manages_dhcp_through_the_mcp(db_session, world, as_client):
    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    network_id = await _joined(db_session, world, as_client, alice, bob)

    got = await _act(alice, ["read"], "dhcp_get", as_client, confirm=False, network_id=network_id)
    assert got["network_id"] == network_id and got["enabled"] is True

    with pytest.raises(ToolError, match="lacks required scope: write"):
        await _act(alice, ["read"], "dhcp_set", as_client, network_id=network_id, enabled=False)
    with pytest.raises(ToolError, match="needs one of"):
        await _act(alice, ["write"], "dhcp_set", as_client, network_id=network_id)
    with pytest.raises(ToolError, match="Invalid DHCP parameters"):
        await _act(alice, ["write"], "dhcp_set", as_client, network_id=network_id, routes="nope")
    done = await _act(alice, ["write"], "dhcp_set", as_client, network_id=network_id,
                      range_start="10.0.0.100", range_end="10.0.0.110", dns_servers=["1.1.1.1"])
    assert done["range_end"] == "10.0.0.110" and done["dns_servers"] == ["1.1.1.1"]
    with pytest.raises(ToolError, match="DHCP range"):
        await _act(alice, ["write"], "dhcp_reservation_add", as_client, network_id=network_id,
                   mac="de:ad:be:ef:00:01", ip="10.0.0.105")

    added = await _act(alice, ["write"], "dhcp_reservation_add", as_client, network_id=network_id,
                       mac="de:ad:be:ef:00:01", ip="10.0.0.50", label="nas")
    rid = added["reservations"][0]["id"]
    updated = await _act(alice, ["write"], "dhcp_reservation_update", as_client, network_id=network_id,
                         reservation_id=rid, ip="10.0.0.51")
    assert updated["reservations"][0]["ip"] == "10.0.0.51"
    with pytest.raises(ToolError, match="lacks required scope: destructive"):
        await _act(alice, ["write"], "dhcp_reservation_remove", as_client, network_id=network_id, reservation_id=rid)
    with pytest.raises(ToolError, match="confirm=true"):
        await _act(alice, ["destructive"], "dhcp_reservation_remove", as_client, confirm=False,
                   network_id=network_id, reservation_id=rid)
    removed = await _act(alice, ["destructive"], "dhcp_reservation_remove", as_client, network_id=network_id,
                         reservation_id=rid)
    assert removed["reservations"] == []

    # Views: the owner sees the full DHCP object, the member the effective settings only.
    owner_view = await _view(alice, as_client)
    assert owner_view["owned"][0]["dhcp"]["range_start"] == "10.0.0.100"
    member_view = await _view(bob, as_client)
    dhcp = member_view["shared"][0]["dhcp"]
    assert dhcp["dns_servers"] == ["1.1.1.1"] and "range_start" not in dhcp and "reservations" not in dhcp


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["member", "stranger"])
async def test_dhcp_sub_actions_are_owner_only(db_session, world, as_client, who):
    alice, bob, carol = _user(db_session, "alice"), _user(db_session, "bob"), _user(db_session, "carol")
    network_id = await _joined(db_session, world, as_client, alice, bob)
    actor = bob if who == "member" else carol
    attempts = [
        ("dhcp_get", ["read"], {}),
        ("dhcp_set", ["write"], {"enabled": False}),
        ("dhcp_reservation_add", ["write"], {"mac": "de:ad:be:ef:00:01", "ip": "10.0.0.50"}),
        ("dhcp_reservation_update", ["write"], {"reservation_id": 1, "ip": "10.0.0.51"}),
        ("dhcp_reservation_remove", ["destructive"], {"reservation_id": 1}),
    ]
    for sub, scopes, params in attempts:
        with pytest.raises(ToolError, match="not found"):
            await _act(actor, scopes, sub, as_client, network_id=network_id, **params)


@pytest.mark.asyncio
async def test_staff_lookup_shows_presence_and_dhcp_summary(
    db_session, world, as_client, mcp_sessionlocal, mcp_auth_ctx_destructive
):
    from app.mcp.tools import combined

    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    network_id = await _joined(db_session, world, as_client, alice, bob)
    await _act(alice, ["write"], "dhcp_reservation_add", as_client, network_id=network_id,
               mac="de:ad:be:ef:00:01", ip="10.0.0.50")
    listed = await combined.lookup(kind="private_networks", location_id=world.location.id)
    row = listed["private_networks"][0]
    assert row["presence"]["node_id"] is None and row["dhcp_warnings"] == []
    assert row["dhcp"]["reservations"] == 1 and row["dhcp"]["enabled"] is True
    assert row["dhcp"]["available"] is False and row["dhcp"]["served_by"] is None


def test_staff_tools_list_stays_within_budget():
    import asyncio
    import json

    import app.mcp.server  # noqa: F401
    from app.mcp.instance import mcp

    tools = asyncio.run(mcp.list_tools())
    size = len(json.dumps([t.model_dump(mode="json", exclude_none=True) for t in tools], separators=(",", ":")))
    assert size < 38_000
