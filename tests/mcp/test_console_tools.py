"""MCP console tool coverage: IPMI/VNC/SOL tickets and virtual media."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.dao.service_dao import ServiceDAO
from app.models.location import Location
from app.models.server import Server
from app.models.service import ProvisioningSource, ServiceStatus
from app.services.ipmi_ticket_service import IPMIProxyUnavailable
from app.services.sol.ticket_service import SolTicketUnavailable
from app.services.virtual_media.base import VirtualMediaUnavailable
from app.services.vm_vnc_ticket_service import VmVncUnavailable


@pytest.fixture
def console_server(db_session):
    loc = Location(name="con", description="")
    db_session.add(loc)
    db_session.flush()
    server = Server(
        name="con-srv",
        server_ip="10.8.8.8",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
        sol_profile="ipmi_sol",
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


@pytest.mark.asyncio
async def test_mint_ipmi_ticket_paths(
    db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, console_server, monkeypatch, test_user
):
    from app.mcp.tools.console import mint_ipmi_ticket

    with pytest.raises(ToolError, match="confirm=true"):
        await mint_ipmi_ticket(server_id=console_server.id, confirm=False)

    with pytest.raises(ToolError, match="Server not found"):
        await mint_ipmi_ticket(server_id=999999, confirm=True)

    with pytest.raises(ToolError, match="Service not found"):
        await mint_ipmi_ticket(service_id=999999, confirm=True)

    monkeypatch.setattr(
        "app.mcp.tools.console.build_launch_payload",
        lambda server: {"launch_url": "https://ipmi.example/launch", "expires_in": 45},
    )
    result = await mint_ipmi_ticket(server_id=console_server.id, confirm=True)
    assert result["url"] == "https://ipmi.example/launch"
    assert result["server_id"] == console_server.id

    bm = ServiceDAO.create_bare_metal(
        db_session,
        name="bm-console",
        server_id=console_server.id,
        owner_user_id=test_user.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
    )
    via_service = await mint_ipmi_ticket(service_id=bm.id, confirm=True)
    assert via_service["server_id"] == console_server.id

    monkeypatch.setattr(
        "app.mcp.tools.console.build_launch_payload",
        lambda server: (_ for _ in ()).throw(IPMIProxyUnavailable("proxy down")),
    )
    with pytest.raises(ToolError, match="proxy down"):
        await mint_ipmi_ticket(server_id=console_server.id, confirm=True)


@pytest.mark.asyncio
async def test_mint_vnc_ticket_paths(
    db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, monkeypatch, test_user, console_server
):
    from app.mcp.tools.console import mint_vnc_ticket

    with pytest.raises(ToolError, match="confirm=true"):
        await mint_vnc_ticket(service_id=1, confirm=False)
    with pytest.raises(ToolError, match="Service not found"):
        await mint_vnc_ticket(service_id=999999, confirm=True)

    bm = ServiceDAO.create_bare_metal(
        db_session,
        name="not-a-vm",
        server_id=console_server.id,
        owner_user_id=test_user.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    with pytest.raises(ToolError, match="Not a VM"):
        await mint_vnc_ticket(service_id=bm.id, confirm=True)

    vm = ServiceDAO.create_vm(
        db_session,
        name="vm-console",
        owner_user_id=test_user.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
    )
    monkeypatch.setattr("app.mcp.tools.console.mint_launch_ticket", lambda sid, console_type=None: "tok")
    monkeypatch.setattr("app.mcp.tools.console.build_launch_url", lambda token: f"https://vnc/{token}")
    result = await mint_vnc_ticket(service_id=vm.id, confirm=True, console_type="serial")
    assert result["url"] == "https://vnc/tok"

    monkeypatch.setattr(
        "app.mcp.tools.console.mint_launch_ticket",
        lambda sid, console_type=None: (_ for _ in ()).throw(VmVncUnavailable("no vnc")),
    )
    with pytest.raises(ToolError, match="no vnc"):
        await mint_vnc_ticket(service_id=vm.id, confirm=True)


@pytest.mark.asyncio
async def test_mint_sol_and_sol_send(
    db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, console_server, monkeypatch
):
    from app.mcp.tools.console import mint_sol_ticket, sol_send

    with pytest.raises(ToolError, match="confirm=true"):
        await mint_sol_ticket(server_id=console_server.id, confirm=False)

    monkeypatch.setattr("app.mcp.tools.console.mint_sol_launch_ticket", lambda sid: "soltok")
    monkeypatch.setattr("app.mcp.tools.console.build_sol_launch_url", lambda token: f"https://sol/{token}")
    minted = await mint_sol_ticket(server_id=console_server.id, confirm=True)
    assert minted["url"] == "https://sol/soltok"

    monkeypatch.setattr(
        "app.mcp.tools.console.mint_sol_launch_ticket",
        lambda sid: (_ for _ in ()).throw(SolTicketUnavailable("sol down")),
    )
    with pytest.raises(ToolError, match="sol down"):
        await mint_sol_ticket(server_id=console_server.id, confirm=True)

    console_server.sol_profile = None
    db_session.commit()
    with pytest.raises(ToolError, match="not configured"):
        await mint_sol_ticket(server_id=console_server.id, confirm=True)

    console_server.sol_profile = "ipmi_sol"
    db_session.commit()
    monkeypatch.setattr(
        "app.mcp.tools.console.inject_bytes",
        AsyncMock(return_value=b"ok"),
    )
    sent = await sol_send(data="hi", server_id=console_server.id, encoding="utf-8", wait_ms=10)
    assert sent["written"] == 2
    assert sent["output"] == "ok"


@pytest.mark.asyncio
async def test_virtual_media_status_insert_eject(
    db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, console_server, monkeypatch
):
    from app.mcp.tools.console import eject_virtual_media, get_virtual_media, insert_virtual_media

    monkeypatch.setattr("app.mcp.tools.console.virtual_media_ready", lambda server: False)
    with pytest.raises(ToolError, match="not configured"):
        await get_virtual_media(server_id=console_server.id)

    monkeypatch.setattr("app.mcp.tools.console.virtual_media_ready", lambda server: True)
    monkeypatch.setattr(
        "app.mcp.tools.console.get_status",
        AsyncMock(return_value={"inserted": False, "filename": None}),
    )
    status = await get_virtual_media(server_id=console_server.id)
    assert status["inserted"] is False

    monkeypatch.setattr(
        "app.mcp.tools.console.get_status",
        AsyncMock(side_effect=VirtualMediaUnavailable("bmc gone")),
    )
    with pytest.raises(ToolError, match="bmc gone"):
        await get_virtual_media(server_id=console_server.id)

    monkeypatch.setattr(
        "app.mcp.tools.console.insert_media",
        AsyncMock(return_value={"inserted": True, "filename": "a.iso"}),
    )
    inserted = await insert_virtual_media(
        filename="a.iso", server_id=console_server.id, boot_once=True, confirm=True
    )
    assert inserted["filename"] == "a.iso"

    monkeypatch.setattr(
        "app.mcp.tools.console.insert_media",
        AsyncMock(side_effect=VirtualMediaUnavailable("mount failed")),
    )
    with pytest.raises(ToolError, match="mount failed"):
        await insert_virtual_media(filename="a.iso", server_id=console_server.id, confirm=True)

    monkeypatch.setattr(
        "app.mcp.tools.console.eject_media",
        AsyncMock(return_value={"inserted": False}),
    )
    ejected = await eject_virtual_media(server_id=console_server.id, confirm=True)
    assert ejected["inserted"] is False

    monkeypatch.setattr(
        "app.mcp.tools.console.eject_media",
        AsyncMock(side_effect=VirtualMediaUnavailable("eject failed")),
    )
    with pytest.raises(ToolError, match="eject failed"):
        await eject_virtual_media(server_id=console_server.id, confirm=True)
