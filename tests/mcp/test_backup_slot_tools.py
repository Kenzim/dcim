"""Backup slots through the existing MCP tools: staff (service_info, restore_backup, provision) and client."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.core.client_mcp_auth import ClientMcpAuthContext
from app.mcp.client import tools as client_tools
from app.mcp.client.context import client_mcp_auth_ctx
from app.mcp.tools import combined
from app.mcp.tools import services as service_tools
from tests.conftest import TestingSessionLocal
from tests.services.test_backup_slots import STORAGE, world  # noqa: F401


@pytest.fixture
def as_client(monkeypatch, mock_redis):
    import app.core.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    monkeypatch.setattr("app.core.database.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.mcp.client.runtime.SessionLocal", TestingSessionLocal)

    def _use(user, scopes):
        client_mcp_auth_ctx.set(
            ClientMcpAuthContext(
                key_id=1, name="t", scopes=scopes, user_id=user.id, username=user.username,
                email=user.email, client_ip="127.0.0.1",
            )
        )

    yield _use
    client_mcp_auth_ctx.set(None)


# --- staff ---


@pytest.mark.asyncio
async def test_staff_provision_slot_view_retain_restore_release(
    world, db_session, mcp_sessionlocal, mcp_auth_ctx_destructive  # noqa: F811
):
    alice = world.users["alice"]
    created = await combined.provision(kind="backup_slot", name="mcp-slot", owner_user_id=alice.id, size_gb=25)
    slot_id = created["id"]
    assert created["service_type"] == "backup_slot"

    with pytest.raises(ToolError, match="owner_user_id"):
        await combined.provision(kind="backup_slot", name="no-owner", size_gb=5)
    with pytest.raises(ToolError, match="does not accept: size_gb"):
        await combined.provision(kind="vm", name="x", product_code="p", vm_template_id=1, size_gb=5)

    volid = world.pbs.add(5100, "a")
    retained = await service_tools.restore_backup(
        service_id=world.vm1.id, volid=volid, action="retain", slot_service_id=slot_id
    )
    assert retained["retained"]["volid"] == volid
    assert (STORAGE, volid, True) in world.pbs.protect_calls

    slot_view = await combined.service_info(slot_id, view="backups")
    assert slot_view["occupied"] is True and slot_view["retained"]["volid"] == volid
    vm_view = await combined.service_info(world.vm1.id, view="backups")
    assert any(b["volid"] == volid and b["retained"] for b in vm_view["backups"])
    assert slot_id in {s["service_id"] for s in vm_view["slots"]}

    with pytest.raises(ToolError, match="confirm"):
        await service_tools.restore_backup(service_id=world.vm2.id, volid=volid)
    with pytest.raises(ToolError, match="same client"):
        await service_tools.restore_backup(service_id=world.bob_vm.id, volid=volid, confirm=True)
    queued = await service_tools.restore_backup(service_id=world.vm2.id, volid=volid, confirm=True)
    assert queued["status"] == "queued" and queued["target_service_id"] == world.vm2.id

    with pytest.raises(ToolError, match="confirm"):
        await service_tools.restore_backup(service_id=slot_id, action="release")
    resized = await service_tools.restore_backup(service_id=slot_id, action="resize", size_gb=40)
    assert resized["size_gb"] == 40
    released = await service_tools.restore_backup(service_id=slot_id, action="release", confirm=True)
    assert released["released"] is True and volid in world.pbs.deleted


# --- client ---


@pytest.mark.asyncio
async def test_client_services_and_service_action_cover_slots(world, db_session, as_client):  # noqa: F811
    alice, bob = world.users["alice"], world.users["bob"]
    as_client(alice, ["read", "write", "destructive"])
    volid = world.pbs.add(5100, "a")

    listed = await client_tools.services(view="list", service_type="backup_slot")
    assert [row["id"] for row in listed] == [world.slot1.id]

    kept = await client_tools.service_action(
        service_id=world.vm1.id, action="backup_retain", volid=volid, slot_service_id=world.slot1.id, confirm=True
    )
    assert kept["retained"]["volid"] == volid
    assert (STORAGE, volid, True) in world.pbs.protect_calls

    slot_view = await client_tools.services(view="backups", service_id=world.slot1.id)
    assert slot_view["retained"]["volid"] == volid
    vm_view = await client_tools.services(view="backups", service_id=world.vm1.id)
    assert world.slot1.id in {s["service_id"] for s in vm_view["slots"]}
    assert any(b["retained"] for b in vm_view["backups"])

    with pytest.raises(ToolError, match="replaces everything"):
        await _restore_without_data_loss_ack(world)
    queued = await client_tools.service_action(
        service_id=world.vm2.id, action="backup_restore", volid=volid, confirm=True
    )
    assert queued["status"] == "queued"

    # Another client's VM and slot look like they do not exist.
    with pytest.raises(ToolError, match="not found"):
        await client_tools.service_action(
            service_id=world.bob_vm.id, action="backup_retain", volid=volid, slot_service_id=world.slot1.id, confirm=True
        )
    as_client(bob, ["read", "write", "destructive"])
    with pytest.raises(ToolError, match="not found"):
        await client_tools.services(view="backups", service_id=world.slot1.id)
    with pytest.raises(ToolError, match="not found"):
        await client_tools.service_action(service_id=world.slot1.id, action="backup_release", confirm=True)

    as_client(alice, ["read", "write", "destructive"])
    freed = await client_tools.service_action(service_id=world.slot1.id, action="backup_release", confirm=True)
    assert freed["released"] is True and volid in world.pbs.deleted


async def _restore_without_data_loss_ack(world):
    # In MCP, confirm=true is also the data-loss acknowledgement, so the unacknowledged path is
    # exercised through the portal handler the tool wraps.
    from app.api import backup_slots as portal
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        auth = {"user_id": world.users["alice"].id, "is_admin": False}
        await portal.client_restore(
            world.slot1.id, portal.RestoreBody(target_service_id=world.vm2.id), auth, db
        )
    except Exception as exc:
        raise ToolError(str(getattr(exc, "detail", exc))) from exc
    finally:
        db.close()


@pytest.mark.asyncio
async def test_client_scopes_for_slot_actions(world, as_client):  # noqa: F811
    as_client(world.users["alice"], ["read"])
    with pytest.raises(ToolError, match="lacks required scope: write"):
        await client_tools.service_action(
            service_id=world.vm1.id, action="backup_retain", volid="v", slot_service_id=world.slot1.id, confirm=True
        )
    as_client(world.users["alice"], ["write"])
    with pytest.raises(ToolError, match="lacks required scope: destructive"):
        await client_tools.service_action(service_id=world.slot1.id, action="backup_release", confirm=True)
