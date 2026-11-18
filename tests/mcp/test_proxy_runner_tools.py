"""Proxy runner MCP tools: key lifecycle and the published-config preview."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.dao.ipam_dao import IPAMDAO
from app.dao.location_dao import LocationDAO
from app.dao.proxy_runner_dao import ProxyRunnerDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType


def _proxy_service(db_session, location, status, suffix):
    server = ServerDAO.create(
        db_session,
        name=f"mcp-proxy-srv-{suffix}",
        server_ip=f"10.61.0.{suffix}",
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    return ServiceDAO.create_bare_metal(
        db_session,
        name=f"mcp-proxy-svc-{suffix}",
        server_id=server.id,
        service_type=ServiceType.HTTP_PROXY,
        status=status,
        provisioning_source=ProvisioningSource.INTERNAL,
    )


@pytest.mark.asyncio
async def test_runner_key_lifecycle(db_session, mcp_sessionlocal, mcp_auth_ctx_destructive):
    from app.mcp.tools.proxy_runners import delete_proxy_runner, proxy_runner, rotate_proxy_runner_key

    created = await proxy_runner(action="create", name="ash-proxy-1")
    first_key = created["api_key"]
    assert first_key and created["enabled"] is True
    row = ProxyRunnerDAO.get_by_id(db_session, created["id"])
    assert ProxyRunnerDAO.verify_api_key(row, first_key, db=db_session)

    listed = await proxy_runner()
    assert [r["name"] for r in listed["runners"]] == ["ash-proxy-1"]
    assert "api_key" not in listed["runners"][0]
    assert "api_key" not in await proxy_runner(runner_id=created["id"])

    updated = await proxy_runner(action="update", runner_id=created["id"], name="ash-proxy-a", enabled=False)
    assert updated["name"] == "ash-proxy-a" and updated["enabled"] is False and "api_key" not in updated

    with pytest.raises(ToolError, match="confirm=true"):
        await rotate_proxy_runner_key(created["id"])
    rotated = await rotate_proxy_runner_key(created["id"], confirm=True)
    db_session.expire_all()
    row = ProxyRunnerDAO.get_by_id(db_session, created["id"])
    assert not ProxyRunnerDAO.verify_api_key(row, first_key, db=db_session)
    assert ProxyRunnerDAO.verify_api_key(row, rotated["api_key"], db=db_session)

    with pytest.raises(ToolError, match="confirm=true"):
        await delete_proxy_runner(created["id"])
    assert (await delete_proxy_runner(created["id"], confirm=True))["deleted"] is True
    with pytest.raises(ToolError, match="not found"):
        await proxy_runner(runner_id=created["id"])


@pytest.mark.asyncio
async def test_runner_argument_checks(mcp_sessionlocal, mcp_auth_ctx_write):
    from app.mcp.tools.proxy_runners import proxy_runner

    with pytest.raises(ToolError, match="needs: name"):
        await proxy_runner(action="create")
    with pytest.raises(ToolError, match="needs: runner_id"):
        await proxy_runner(action="update", name="x")


@pytest.mark.asyncio
async def test_runner_writes_need_write_scope(mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.proxy_runners import proxy_runner

    with pytest.raises(ToolError, match="scope"):
        await proxy_runner(action="create", name="nope")


@pytest.mark.asyncio
async def test_rotate_needs_destructive_scope(mcp_sessionlocal, mcp_auth_ctx_write):
    from app.mcp.tools.proxy_runners import rotate_proxy_runner_key

    with pytest.raises(ToolError, match="scope"):
        await rotate_proxy_runner_key(1, confirm=True)


@pytest.mark.asyncio
async def test_published_view_matches_runner_config(db_session, mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.proxy_runners import proxy_runner

    location = LocationDAO.create(db_session, name="loc-mcp-proxy")
    ProxyRunnerDAO.create(db_session, name="r1", api_key="k1")
    active = _proxy_service(db_session, location, ServiceStatus.ACTIVE, "1")
    suspended = _proxy_service(db_session, location, ServiceStatus.SUSPENDED, "2")
    subnet = IPAMDAO.create_subnet(db_session, name="mcp-pool", cidr="198.51.100.16/29", location_id=location.id)
    live = IPAMDAO.assign_ip(db_session, service_id=active.id, subnet_id=subnet.id, username="u1", password="p1")
    IPAMDAO.assign_ip(db_session, service_id=suspended.id, subnet_id=subnet.id, username="u2", password="p2")

    view = await proxy_runner(action="published")
    assert view["published"] == 1 and view["published_by_subnet"] == {"mcp-pool": 1}
    assert view["entries"] == [{"service_id": active.id, "bind_ip": live.ip.ip_address, "subnet": "mcp-pool"}]
    assert view["held_back_by_reason"] == {"service_suspended": 1}
    assert view["runners"] == {"enabled": 1, "online": 0}
    text = str(view)
    assert "p1" not in text and "u1" not in text

    only = await proxy_runner(action="published", service_id=suspended.id)
    assert only["entries"] == [] and only["held_back_entries"][0]["reason"] == "service_suspended"


@pytest.mark.asyncio
async def test_fast_path_placement_pinning_and_view(db_session, mcp_sessionlocal, mcp_auth_ctx_write):
    from app.dao.private_network_dao import PrivateNetworkDAO
    from app.mcp.tools.combined import save_subnet
    from app.mcp.tools.proxy_runners import proxy_runner

    location = LocationDAO.create(db_session, name="loc-mcp-fast")
    pool = PrivateNetworkDAO.create_pool(
        db_session, location_id=location.id, vlan_start=2100, vlan_end=2109, bridge="vmbr0",
        max_networks_per_client=2, max_networks_per_service=4,
    )
    pool.proxy_access_enabled = True
    db_session.commit()
    runner, _ = ProxyRunnerDAO.create(db_session, name="fast-1", api_key="kf")
    subnet = IPAMDAO.create_subnet(db_session, name="fast-pool", cidr="198.51.100.32/29", location_id=location.id)

    with pytest.raises(ToolError, match="location"):
        await save_subnet(subnet_id=subnet.id, proxy_runner_id=runner.id)
    with pytest.raises(ToolError, match="does not accept: proxy_runner_id"):
        await save_subnet(name="x", cidr="198.51.100.40/29", proxy_runner_id=runner.id)

    placed = await proxy_runner(action="update", runner_id=runner.id, location_id=location.id)
    assert placed["location_id"] == location.id and placed["vlan_mode"] == "preconfigured"
    pinned = await save_subnet(subnet_id=subnet.id, proxy_runner_id=runner.id)
    assert pinned["fast_path"]["ip"] == "100.80.0.1" and pinned["proxy_runner_name"] == "fast-1"

    view = await proxy_runner(action="fast_path", runner_id=runner.id)
    assert view["cidr"] == "100.80.0.0/16"
    assert view["runners"][0]["active"] is True and view["runners"][0]["tenants"] == []

    cleared = await proxy_runner(action="update", runner_id=runner.id, clear_placement=True)
    assert cleared["location_id"] is None
    assert (await proxy_runner(action="fast_path"))["runners"][0]["active"] is False
    with pytest.raises(ToolError, match="not both"):
        await proxy_runner(action="update", runner_id=runner.id, clear_placement=True, location_id=location.id)
