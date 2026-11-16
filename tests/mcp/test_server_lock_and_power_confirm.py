"""MCP: server lock enforcement and power confirmation."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.mcp.tools import combined
from app.models.location import Location
from app.models.server import Server


@pytest.fixture
def locked_server(db_session):
    loc = Location(name="mcp-lock-loc", description="")
    db_session.add(loc)
    db_session.flush()
    server = Server(
        name="mcp-lock-srv",
        server_ip="10.8.8.8",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
        locked=True,
        lock_reason="customer data",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


@pytest.mark.asyncio
async def test_mcp_reinstall_refused(db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, locked_server, monkeypatch):
    from app.mcp.tools import power
    from app.models.service import Service, ServiceType

    monkeypatch.setattr(
        power.ServiceDAO,
        "get_by_server",
        lambda db, sid: [Service(name="bm", service_type=ServiceType.BARE_METAL)],
    )
    monkeypatch.setattr("app.api.billing.service_linked_server", lambda db, service: locked_server)
    with pytest.raises(ToolError, match="Server is locked: customer data"):
        await power.server_reinstall_os(locked_server.id, "debian-12", confirm=True)


@pytest.mark.asyncio
async def test_mcp_cannot_unlock_with_write_scope(db_session, mcp_sessionlocal, mcp_auth_ctx_write, locked_server):
    with pytest.raises(ToolError, match="cannot be unlocked"):
        await combined.save_server(server_id=locked_server.id, locked=False)
    row = await combined.save_server(server_id=locked_server.id, lock_reason="new reason")
    assert row["locked"] is True and row["lock_reason"] == "new reason"


@pytest.mark.asyncio
async def test_mcp_cannot_unlock_even_with_destructive_scope(
    db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, locked_server
):
    with pytest.raises(ToolError, match="cannot be unlocked"):
        await combined.save_server(server_id=locked_server.id, locked=False)
    db_session.refresh(locked_server)
    assert locked_server.locked is True


@pytest.mark.asyncio
async def test_mcp_can_lock_unlocked_server(db_session, mcp_sessionlocal, mcp_auth_ctx_write, locked_server):
    locked_server.locked = False
    locked_server.lock_reason = None
    db_session.commit()
    row = await combined.save_server(server_id=locked_server.id, locked=True, lock_reason="hypervisor")
    assert row["locked"] is True and row["lock_reason"] == "hypervisor"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["on", "off", "reset", "cycle"])
async def test_server_power_needs_confirm_for_every_action(
    db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, action
):
    with pytest.raises(ToolError, match="confirm"):
        await combined.power(action, server_id=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["on", "cycle"])
async def test_service_power_needs_confirm_for_on_and_cycle(
    db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, action
):
    with pytest.raises(ToolError, match="confirm"):
        await combined.power(action, service_id=1)


@pytest.mark.asyncio
async def test_mcp_sets_and_clears_profiles(db_session, mcp_sessionlocal, mcp_auth_ctx_write, locked_server):
    kinds = {k: await combined.lookup(kind=k) for k in ("kvm_profiles", "sol_profiles", "virtual_media_profiles")}
    assert all(r["profiles"] for r in kinds.values())
    row = await combined.save_server(
        server_id=locked_server.id,
        ipmi_kvm_profile="gigabyte",
        sol_profile="ipmi_sol",
        virtual_media_profile="megarac_stream",
    )
    assert (row["ipmi_kvm_profile"], row["sol_profile"], row["virtual_media_profile"]) == (
        "gigabyte",
        "ipmi_sol",
        "megarac_stream",
    )
    assert row["locked"] is True
    with pytest.raises(ToolError, match="virtual_media_profile"):
        await combined.save_server(server_id=locked_server.id, virtual_media_profile="no-such-profile")
    row = await combined.save_server(server_id=locked_server.id, sol_profile="")
    assert row["sol_profile"] is None and row["ipmi_kvm_profile"] == "gigabyte"
