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
async def test_create_update_and_inspect_subnet(db_session, mcp_sessionlocal, mcp_auth_ctx_write):
    from app.mcp.tools.ipam import create_subnet, get_subnet, update_subnet
    from app.models.location import Location

    loc = Location(name="subnet-dc", description="")
    db_session.add(loc)
    db_session.commit()

    created = await create_subnet(
        "proxy-a", "10.20.0.5/29", location_id=loc.id, range_start="10.20.0.2", tags=["proxy"]
    )
    assert created["cidr"] == "10.20.0.0/29"
    assert created["range_start"] == "10.20.0.2"
    assert created["total_ips"] == 5
    assert created["enabled"] is True

    detail = await get_subnet(created["id"], limit=2)
    assert [ip["ip_address"] for ip in detail["ips"]] == ["10.20.0.2", "10.20.0.3"]
    assert detail["ips_matching"] == 5 and detail["ips_shown"] == 2

    updated = await update_subnet(created["id"], enabled=False, clear_location=True, max_resale_count=3)
    assert updated["enabled"] is False
    assert updated["location_id"] is None
    assert updated["max_resale_count"] == 3

    disabled = await create_subnet("proxy-b", "10.20.1.0/30", enabled=False)
    assert disabled["enabled"] is False and disabled["total_ips"] == 2


@pytest.mark.asyncio
async def test_create_subnet_validation(db_session, mcp_sessionlocal, mcp_auth_ctx_write):
    from app.mcp.tools.ipam import create_subnet, update_subnet

    with pytest.raises(ToolError, match="Invalid CIDR"):
        await create_subnet("bad", "10.0.0.0/33")
    with pytest.raises(ToolError, match="outside"):
        await create_subnet("bad", "10.30.0.0/29", range_end="10.31.0.1")
    with pytest.raises(ToolError, match="Location 424242 not found"):
        await create_subnet("bad", "10.30.0.0/29", location_id=424242)
    first = await create_subnet("wide", "10.30.0.0/28")
    with pytest.raises(ToolError, match=f"overlaps subnet {first['id']}"):
        await create_subnet("inner", "10.30.0.8/29")
    with pytest.raises(ToolError, match="Subnet not found"):
        await update_subnet(999999, name="x")


@pytest.mark.asyncio
async def test_create_subnet_needs_write_scope(mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.ipam import create_subnet

    with pytest.raises(ToolError, match="scope"):
        await create_subnet("ro", "10.40.0.0/30")


@pytest.mark.asyncio
async def test_delete_subnet_requires_confirm(
    mcp_sessionlocal, mcp_auth_ctx_destructive
):
    from app.mcp.tools.ipam import delete_subnet

    with pytest.raises(ToolError, match="confirm"):
        await delete_subnet(1, confirm=False)
