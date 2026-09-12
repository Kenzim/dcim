"""MCP IPAM tool tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User


@pytest.mark.asyncio
async def test_list_subnets_and_assignments_empty(
    db_session, mcp_sessionlocal, mcp_auth_ctx_read
):
    from app.mcp.tools.ipam import list_ip_assignments, list_subnets, list_vm_ips

    subnets = await list_subnets()
    assert subnets["subnets"] == []

    assignments = await list_ip_assignments()
    assert assignments["assignments"] == []

    vm_ips = await list_vm_ips()
    assert vm_ips["vm_ips"] == []


@pytest.mark.asyncio
async def test_assign_ip_rejects_non_proxy(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write
):
    user = User(username="ipam-user", email="ipam@example.com", is_admin=False)
    user.set_password("password123")
    db_session.add(user)
    db_session.flush()
    service = Service(
        owner_user_id=user.id,
        name="vm-svc",
        service_type=ServiceType.VM,
        status=ServiceStatus.ACTIVE,
    )
    db_session.add(service)
    db_session.commit()
    db_session.refresh(service)

    from app.mcp.tools.ipam import assign_ip

    with pytest.raises(ToolError, match="http_proxy"):
        await assign_ip(service.id)


@pytest.mark.asyncio
async def test_release_ip_not_found(db_session, mcp_sessionlocal, mcp_auth_ctx_write):
    from app.mcp.tools.ipam import release_ip

    with pytest.raises(ToolError, match="not found"):
        await release_ip(999999)


@pytest.mark.asyncio
async def test_rotate_ip_credentials_requires_confirm(
    mcp_sessionlocal, mcp_auth_ctx_destructive
):
    from app.mcp.tools.ipam import rotate_ip_credentials

    with pytest.raises(ToolError, match="confirm"):
        await rotate_ip_credentials(1, confirm=False)


@pytest.mark.asyncio
async def test_delete_subnet_requires_confirm(
    mcp_sessionlocal, mcp_auth_ctx_destructive
):
    from app.mcp.tools.ipam import delete_subnet

    with pytest.raises(ToolError, match="confirm"):
        await delete_subnet(1, confirm=False)
