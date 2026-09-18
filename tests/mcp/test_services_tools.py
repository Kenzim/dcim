"""MCP services tool read-path tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User


@pytest.fixture
def service_row(db_session):
    user = User(username="svc-user", email="svc@example.com", is_admin=False)
    user.set_password("password123")
    db_session.add(user)
    db_session.flush()
    svc = Service(
        owner_user_id=user.id,
        name="svc-1",
        service_type=ServiceType.VM,
        status=ServiceStatus.ACTIVE,
        config={},
    )
    db_session.add(svc)
    db_session.commit()
    db_session.refresh(svc)
    return svc


@pytest.mark.asyncio
async def test_list_services_and_get_service(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read, service_row
):
    from app.mcp.tools.services import get_service, list_services

    listed = await list_services(limit=10)
    ids = {row["id"] for row in listed["services"]}
    assert service_row.id in ids

    detail = await get_service(service_row.id)
    assert detail["id"] == service_row.id
    assert detail["name"] == "svc-1"


@pytest.mark.asyncio
async def test_get_service_not_found(db_session, mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.services import get_service

    with pytest.raises(ToolError, match="Service not found"):
        await get_service(999999)


@pytest.mark.asyncio
async def test_list_service_backups_and_jobs(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read, service_row, monkeypatch
):
    async def fake_backups(db, row):
        return ([{"id": 1, "name": "snap"}], [])

    job = SimpleNamespace(
        id=5,
        status=SimpleNamespace(value="completed"),
        strategy_name="linux",
        attempt=1,
        error_message=None,
        created_at=None,
    )
    monkeypatch.setattr(
        "app.mcp.tools.services.list_service_backups_and_jobs",
        fake_backups,
    )
    monkeypatch.setattr(
        "app.mcp.tools.services.VMDeploymentJobDAO.list_by_service",
        lambda db, service_id, limit=20: [job],
    )

    from app.mcp.tools.services import list_deployment_jobs, list_service_backups

    backups = await list_service_backups(service_row.id)
    assert backups["backups"][0]["name"] == "snap"

    jobs = await list_deployment_jobs(service_row.id)
    assert jobs["jobs"][0]["id"] == 5


@pytest.mark.asyncio
async def test_service_power_off_requires_confirm(
    db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, service_row
):
    from app.mcp.tools.services import service_power

    with pytest.raises(ToolError, match="confirm"):
        await service_power(service_row.id, action="off", confirm=False)


@pytest.mark.asyncio
async def test_list_filter_provision_and_lifecycle_errors(
    db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, service_row, monkeypatch
):
    from app.mcp.tools.services import (
        list_deployment_jobs,
        list_service_backups,
        list_services,
        provision_bare_metal,
        provision_http_proxy,
        provision_vm,
        restore_backup,
        service_power,
        service_set_status,
        terminate_service,
    )
    from app.services.provisioning import ProvisioningError

    listed = await list_services(service_type="vm", status="active", limit=5)
    assert any(row["id"] == service_row.id for row in listed["services"])

    with pytest.raises(ToolError, match="Service not found"):
        await list_service_backups(999999)
    with pytest.raises(ToolError, match="Service not found"):
        await list_deployment_jobs(999999)

    monkeypatch.setattr(
        "app.mcp.tools.services.ProvisioningService.create",
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(ProvisioningError("invalid", "nope"))),
    )
    with pytest.raises(ToolError, match="nope"):
        await provision_vm(name="x", product_code="p", vm_template_id=1)

    monkeypatch.setattr(
        "app.mcp.tools.services.ProvisioningService.create",
        staticmethod(lambda db, req, actor: service_row),
    )
    monkeypatch.setattr("app.mcp.tools.services.service_row", lambda db, s: {"id": s.id, "name": s.name})
    created = await provision_vm(name="x", product_code="p", vm_template_id=1)
    assert created["id"] == service_row.id
    assert (await provision_http_proxy(name="proxy"))["id"] == service_row.id
    assert (await provision_bare_metal(name="bm", server_id=1))["id"] == service_row.id

    with pytest.raises(ToolError, match="Service not found"):
        await service_power(999999, action="on")

    monkeypatch.setattr("app.mcp.tools.services.admin_vm_power_action", AsyncMock(return_value=None))
    powered = await service_power(service_row.id, action="on")
    assert powered["id"] == service_row.id

    with pytest.raises(ToolError, match="confirm"):
        await terminate_service(service_row.id, confirm=False)
    with pytest.raises(ToolError, match="confirm"):
        await restore_backup(service_row.id, volid="pbs:vm/1", confirm=False)
    with pytest.raises(ToolError, match="confirm"):
        await service_set_status(service_row.id, status="terminated", confirm=False)

    monkeypatch.setattr(
        "app.mcp.tools.services.update_service_status",
        AsyncMock(return_value=None),
    )
    statused = await service_set_status(service_row.id, status="suspended")
    assert statused["id"] == service_row.id

    monkeypatch.setattr(
        "app.mcp.tools.services.admin_restore_vm_backup",
        AsyncMock(return_value={"ok": True}),
    )
    restored = await restore_backup(service_row.id, volid="pbs:vm/1", confirm=True)
    assert restored["ok"] is True

    from app.mcp.tools.services import reinstall_vm

    monkeypatch.setattr(
        "app.mcp.tools.services.admin_reinstall_vm_guest",
        AsyncMock(return_value=None),
    )
    reinstalled = await reinstall_vm(service_row.id, vm_template_id=3, confirm=True)
    assert reinstalled["id"] == service_row.id

    from app.models.service import ServiceType

    service_row.service_type = ServiceType.BARE_METAL
    db_session.commit()
    monkeypatch.setattr("app.services.service_resource.service_linked_server", lambda db, s: None)
    with pytest.raises(ToolError, match="no linked server"):
        await service_power(service_row.id, action="on")
    monkeypatch.setattr(
        "app.services.service_resource.service_linked_server",
        lambda db, s: SimpleNamespace(id=99),
    )
    monkeypatch.setattr(
        "app.mcp.tools.power.server_power",
        AsyncMock(return_value={"power": "on"}),
    )
    bm_power = await service_power(service_row.id, action="on")
    assert bm_power["power"] == "on"

