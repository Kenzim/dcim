"""Client and staff MCP for shared private networks: owner-only actions, member actions and removal."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.dao.private_network_dao import PrivateNetworkDAO
from app.mcp.client import tools
from app.mcp.client.context import client_mcp_auth_ctx
from app.services.private_network_service import PrivateNetworkService
from tests.api.test_private_network_sharing_api import _user, _vm, proxmox, world  # noqa: F401  (fixtures)
from tests.mcp.test_client_mcp import as_client, cmcp_db, rate_limit_redis  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _reset_context():
    yield
    client_mcp_auth_ctx.set(None)


async def _act(user, scopes, sub, as_client, service_id=None, confirm=True, **params):
    as_client(user, scopes)
    return await tools.service_action(action="network", name=sub, service_id=service_id, params=params, confirm=confirm)


async def _owned_network(db, owner, world):
    network = await PrivateNetworkService.create_network(
        db, owner_user_id=owner.id, location_id=world.location.id, name="backend"
    )
    return network.id


async def _joined(db, world, as_client, alice, bob):
    network_id = await _owned_network(db, alice, world)
    await _act(alice, ["write"], "invite", as_client, network_id=network_id, invitee="bob")
    view = await _view(bob, as_client)
    await _act(bob, ["write"], "invitation_accept", as_client, invitation_id=view["invitations"][0]["id"])
    return network_id


async def _view(user, as_client, scopes=("read",)):
    as_client(user, list(scopes))
    return await tools.services(view="networks")


@pytest.mark.asyncio
async def test_invite_accept_attach_and_leave_through_the_mcp(db_session, world, as_client):
    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    bob_vm = _vm(db_session, bob, world.location, "bob-vm")
    network_id = await _joined(db_session, world, as_client, alice, bob)

    view = await _view(bob, as_client)
    assert view["owned"] == [] and view["invitations"] == []
    shared = view["shared"][0]
    assert shared["id"] == network_id and shared["owner"] == "alice"
    assert "vlan_id" not in shared and shared["storage"]["linked"] is False

    attached = await _act(bob, ["write"], "attach", as_client, service_id=bob_vm.id, network_id=network_id)
    assert [a["service_name"] for a in attached["attachments"]] == ["bob-vm"]
    owner_view = await _view(alice, as_client)
    assert owner_view["owned"][0]["members"][0]["servers"][0]["ip_address"] == attached["attachments"][0]["ip_address"]

    gone = await _act(bob, ["destructive"], "detach", as_client, network_id=network_id, attachment_id=attached["attachments"][0]["id"])
    assert gone["attachments"] == []
    await _act(bob, ["write"], "attach", as_client, service_id=bob_vm.id, network_id=network_id)
    assert await _act(bob, ["destructive"], "leave", as_client, network_id=network_id) == {"left": True}
    assert (bob_vm.vm.proxmox_vmid, 2000) not in world.proxmox.nics
    assert (await _view(bob, as_client))["shared"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["member", "stranger"])
async def test_owner_only_actions_are_refused_through_the_mcp(db_session, world, as_client, who):
    alice, bob, carol = _user(db_session, "alice"), _user(db_session, "bob"), _user(db_session, "carol")
    network_id = await _joined(db_session, world, as_client, alice, bob)
    pending = (await _act(alice, ["write"], "invite", as_client, network_id=network_id, invitee="x@example.com"))["id"]
    actor = bob if who == "member" else carol
    mine = _vm(db_session, actor, world.location, "actor-vm")

    attempts = [
        ("invite", ["write"], {"network_id": network_id, "invitee": "carol"}),
        ("invitation_cancel", ["write"], {"network_id": network_id, "invitation_id": pending}),
        ("member_remove", ["destructive"], {"network_id": network_id, "member_user_id": bob.id}),
    ]
    for sub, scopes, params in attempts:
        with pytest.raises(ToolError, match="not found"):
            await _act(actor, scopes, sub, as_client, **params)
    if who == "stranger":
        with pytest.raises(ToolError, match="not found"):
            await _act(actor, ["write"], "attach", as_client, service_id=mine.id, network_id=network_id)
        with pytest.raises(ToolError, match="not found"):
            await _act(actor, ["destructive"], "leave", as_client, network_id=network_id)
    # An owner view is the owner's alone: neither a member nor a stranger lists it.
    assert (await _view(actor, as_client))["owned"] == []
    db_session.expire_all()
    assert PrivateNetworkDAO.count_members(db_session, network_id) == 1
    assert PrivateNetworkDAO.count_pending_invitations(db_session, network_id) == 1


@pytest.mark.asyncio
async def test_member_actions_are_refused_to_strangers_through_the_mcp(db_session, world, as_client):
    alice, bob, carol = _user(db_session, "alice"), _user(db_session, "bob"), _user(db_session, "carol")
    bob_vm = _vm(db_session, bob, world.location, "bob-vm")
    carol_vm = _vm(db_session, carol, world.location, "carol-vm")
    network_id = await _joined(db_session, world, as_client, alice, bob)
    attached = await _act(bob, ["write"], "attach", as_client, service_id=bob_vm.id, network_id=network_id)
    attachment_id = attached["attachments"][0]["id"]

    with pytest.raises(ToolError, match="not found"):
        await _act(carol, ["write"], "attach", as_client, service_id=carol_vm.id, network_id=network_id)
    with pytest.raises(ToolError, match="not found"):
        await _act(carol, ["destructive"], "detach", as_client, network_id=network_id, attachment_id=attachment_id)
    with pytest.raises(ToolError, match="not found"):
        await _act(carol, ["destructive"], "leave", as_client, network_id=network_id)
    with pytest.raises(ToolError, match="not found"):
        await _act(carol, ["write"], "invitation_accept", as_client, invitation_id=1)
    view = await _view(carol, as_client)
    assert view == {"owned": [], "shared": [], "invitations": []}
    assert (bob_vm.vm.proxmox_vmid, 2000) in world.proxmox.nics
    # A member cannot attach someone else's service either.
    with pytest.raises(ToolError, match="not found"):
        await _act(bob, ["write"], "attach", as_client, service_id=carol_vm.id, network_id=network_id)


@pytest.mark.asyncio
async def test_scopes_and_confirm_apply_to_network_actions(db_session, world, as_client):
    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    network_id = await _joined(db_session, world, as_client, alice, bob)
    with pytest.raises(ToolError, match="lacks required scope: write"):
        await _act(alice, ["read"], "invite", as_client, network_id=network_id, invitee="carol")
    with pytest.raises(ToolError, match="lacks required scope: destructive"):
        await _act(alice, ["write"], "member_remove", as_client, network_id=network_id, member_user_id=bob.id)
    with pytest.raises(ToolError, match="confirm=true"):
        await _act(alice, ["destructive"], "member_remove", as_client, confirm=False, network_id=network_id, member_user_id=bob.id)
    with pytest.raises(ToolError, match="needs name"):
        await _act(alice, ["destructive"], "nonsense", as_client)
    with pytest.raises(ToolError, match="needs: service_id"):
        as_client(alice, ["write"])
        await tools.service_action(action="power_on", confirm=True)


@pytest.mark.asyncio
async def test_owner_removes_a_member_through_the_mcp(db_session, world, as_client):
    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    bob_vm = _vm(db_session, bob, world.location, "bob-vm")
    network_id = await _joined(db_session, world, as_client, alice, bob)
    await _act(bob, ["write"], "attach", as_client, service_id=bob_vm.id, network_id=network_id)
    assert (bob_vm.vm.proxmox_vmid, 2000) in world.proxmox.nics

    assert await _act(alice, ["destructive"], "member_remove", as_client, network_id=network_id, member_user_id=bob.id) == {
        "removed": True
    }
    assert (bob_vm.vm.proxmox_vmid, 2000) not in world.proxmox.nics
    assert (await _view(bob, as_client))["shared"] == []
    with pytest.raises(ToolError, match="not found"):
        await _act(bob, ["write"], "attach", as_client, service_id=bob_vm.id, network_id=network_id)


@pytest.mark.asyncio
async def test_staff_lists_members_and_removes_one(db_session, world, as_client, mcp_sessionlocal, mcp_auth_ctx_destructive):
    from app.mcp.tools import combined

    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    bob_vm = _vm(db_session, bob, world.location, "bob-vm")
    network_id = await _joined(db_session, world, as_client, alice, bob)
    await _act(bob, ["write"], "attach", as_client, service_id=bob_vm.id, network_id=network_id)

    listed = await combined.lookup(kind="private_networks", location_id=world.location.id)
    members = listed["private_networks"][0]["members"]
    assert [m["username"] for m in members] == ["bob"] and members[0]["servers"][0]["kind"] == "vm"

    with pytest.raises(ToolError, match="confirm"):
        await combined.switch_action(action="network_member_remove", network_id=network_id, member_user_id=bob.id)
    with pytest.raises(ToolError, match="needs"):
        await combined.switch_action(action="network_member_remove", network_id=network_id, confirm=True)
    removed = await combined.switch_action(
        action="network_member_remove", network_id=network_id, member_user_id=bob.id, confirm=True
    )
    assert removed["removed"] is True
    assert (bob_vm.vm.proxmox_vmid, 2000) not in world.proxmox.nics
    db_session.expire_all()
    assert PrivateNetworkDAO.count_members(db_session, network_id) == 0


def test_tool_names_and_sizes_are_unchanged_by_sharing():
    import asyncio
    import json

    import app.mcp.server  # noqa: F401
    from app.mcp.client import server  # noqa: F401
    from app.mcp.client.instance import client_mcp
    from app.mcp.instance import mcp

    assert len(asyncio.run(mcp.list_tools())) == 68
    listed = client_mcp._tool_manager.list_tools()
    assert sorted(t.name for t in listed) == ["account", "billing", "service_action", "services", "storage", "tickets"]
    size = len(
        json.dumps(
            [{"name": t.name, "description": t.description, "inputSchema": t.parameters} for t in listed],
            separators=(",", ":"),
            default=str,
        )
    )
    assert size < 8_000
