"""MCP DHCP/TFTP tool tests with mocked runners."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.models.location import Location
from app.models.service_instance import ServiceInstance


@pytest.fixture
def dhcp_location(db_session):
    loc = Location(name="dhcp-mcp", description="")
    db_session.add(loc)
    db_session.flush()
    inst = ServiceInstance(
        location_id=loc.id,
        service_type="dhcp",
        name="dhcp",
        base_url="http://dhcp.local",
        api_key_encrypted="test-key",
    )
    db_session.add(inst)
    db_session.commit()
    db_session.refresh(loc)
    return loc


@pytest.mark.asyncio
async def test_dhcp_tftp_status_dhcp_and_tftp(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read, dhcp_location, monkeypatch
):
    instance = SimpleNamespace(id=1)
    monkeypatch.setattr(
        "app.mcp.tools.dhcp_tftp._get_dhcp_instance",
        lambda db, location_id: instance,
    )
    monkeypatch.setattr(
        "app.mcp.tools.dhcp_tftp._get_tftp_instance",
        lambda db, location_id: instance,
    )
    monkeypatch.setattr(
        "app.mcp.tools.dhcp_tftp.call_dhcp_runner",
        AsyncMock(return_value=(200, {"running": True})),
    )
    monkeypatch.setattr(
        "app.mcp.tools.dhcp_tftp.call_tftp_runner",
        AsyncMock(return_value=(200, {"running": True})),
    )
    from app.mcp.tools.dhcp_tftp import dhcp_tftp_status

    dhcp = await dhcp_tftp_status(dhcp_location.id, kind="dhcp")
    assert dhcp["kind"] == "dhcp"
    tftp = await dhcp_tftp_status(dhcp_location.id, kind="tftp")
    assert tftp["kind"] == "tftp"


@pytest.mark.asyncio
async def test_dhcp_tftp_status_invalid_kind(
    mcp_sessionlocal, mcp_auth_ctx_read, dhcp_location
):
    from app.mcp.tools.dhcp_tftp import dhcp_tftp_status

    with pytest.raises(ToolError):
        await dhcp_tftp_status(dhcp_location.id, kind="invalid")


@pytest.mark.asyncio
async def test_dhcp_tftp_settings_paths(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read, dhcp_location, monkeypatch
):
    instance = SimpleNamespace(id=7)
    cfg = SimpleNamespace(model_dump=lambda: {"path": "/shared/dhcp/dhcpd.conf"})
    config_service = MagicMock()
    config_service.get_config_by_service_instance.return_value = cfg
    monkeypatch.setattr(
        "app.mcp.tools.dhcp_tftp.get_dhcp_config_service",
        lambda: config_service,
    )
    monkeypatch.setattr(
        "app.mcp.tools.dhcp_tftp._get_dhcp_instance",
        lambda db, location_id: instance,
    )
    monkeypatch.setattr(
        "app.mcp.tools.dhcp_tftp._get_tftp_instance",
        lambda db, location_id: instance,
    )
    monkeypatch.setattr(
        "app.mcp.tools.dhcp_tftp.call_tftp_runner",
        AsyncMock(return_value=(200, {"root": "/tftp"})),
    )
    from app.mcp.tools.dhcp_tftp import dhcp_tftp_settings

    dhcp = await dhcp_tftp_settings(dhcp_location.id, kind="dhcp")
    assert dhcp["settings"]["path"] == "/shared/dhcp/dhcpd.conf"
    tftp = await dhcp_tftp_settings(dhcp_location.id, kind="tftp")
    assert tftp["settings"]["root"] == "/tftp"


@pytest.mark.asyncio
async def test_dhcp_tftp_logs_and_control(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read, mcp_auth_ctx_destructive, dhcp_location, monkeypatch
):
    instance = SimpleNamespace(id=3)
    monkeypatch.setattr(
        "app.mcp.tools.dhcp_tftp._get_dhcp_instance",
        lambda db, location_id: instance,
    )
    monkeypatch.setattr(
        "app.mcp.tools.dhcp_tftp.call_dhcp_runner",
        AsyncMock(return_value=(200, ["line1"])),
    )
    monkeypatch.setattr(
        "app.mcp.tools.dhcp_tftp._dhcp_tftp_runner_action",
        AsyncMock(return_value={"success": True, "kind": "dhcp", "action": "restart"}),
    )
    from app.mcp.tools.dhcp_tftp import dhcp_tftp_control, dhcp_tftp_logs

    logs = await dhcp_tftp_logs(dhcp_location.id, kind="dhcp", limit=10)
    assert logs["logs"] == ["line1"]
    with pytest.raises(ToolError, match="confirm=true"):
        await dhcp_tftp_control(dhcp_location.id, kind="dhcp", action="restart", confirm=False)
    restarted = await dhcp_tftp_control(
        dhcp_location.id, kind="dhcp", action="restart", confirm=True
    )
    assert restarted["action"] == "restart"
