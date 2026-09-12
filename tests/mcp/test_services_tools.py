"""MCP services tool read-path tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

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
