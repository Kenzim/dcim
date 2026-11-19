"""Staff MCP storage actions for failover: drain, undrain, location, rebalance and move."""

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tests.services.test_storage_failover import World


@pytest.fixture
def world(db_session, monkeypatch):
    from cryptography.fernet import Fernet

    from app.core.config import settings

    monkeypatch.setattr(settings, "service_instance_encryption_key", Fernet.generate_key().decode())
    return World(db_session)


@pytest.mark.asyncio
async def test_drain_undrain_and_move(world, mcp_sessionlocal, mcp_auth_ctx_destructive):
    from app.mcp.tools.storage import storage, storage_node

    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=a)
    moved = await storage(action="move", service_id=tenant.service_id, node_id=b.id)
    assert moved["move"]["to_node_id"] == b.id
    with pytest.raises(ToolError, match="node_id"):
        await storage(action="move", service_id=tenant.service_id)

    drained = await storage_node(action="drain", node_id=b.id)
    assert drained["draining"] is True
    assert (await storage_node(action="undrain", node_id=b.id))["draining"] is False
    with pytest.raises(ToolError, match="node_id"):
        await storage_node(action="drain")


@pytest.mark.asyncio
async def test_location_view_update_and_rebalance(world, mcp_sessionlocal, mcp_auth_ctx_destructive):
    from app.mcp.tools.storage import storage_node

    world.node(sftp=True)
    world.node(sftp=True)
    for _ in range(4):
        world.tenant(world.nodes[0], current=world.nodes[0])
    view = await storage_node(action="location", location_id=world.location.id)
    assert view["ha_active"] is True and view["sftp_host_key_set"] is False and "events" in view
    updated = await storage_node(
        action="location", location_id=world.location.id, sftp_enabled=True, sftp_host="files.example.com",
        sftp_port=2200, sftp_virtual_ip="198.51.100.9/24", ha_enabled=True, auto_failback=False,
    )
    assert updated["sftp_host"] == "files.example.com" and updated["sftp_port"] == 2200
    assert updated["sftp_virtual_ip"] == "198.51.100.9/24" and updated["auto_failback"] is False
    assert "PRIVATE KEY" not in json.dumps(updated)
    cleared = await storage_node(action="location", location_id=world.location.id, sftp_virtual_ip="")
    assert cleared["sftp_virtual_ip"] is None
    rebalanced = await storage_node(action="rebalance", location_id=world.location.id)
    assert len(rebalanced["changed"]) == 2
    with pytest.raises(ToolError, match="location_id"):
        await storage_node(action="rebalance")


@pytest.mark.asyncio
async def test_failover_writes_need_write_scope(mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.storage import storage, storage_node

    with pytest.raises(ToolError, match="scope"):
        await storage_node(action="drain", node_id=1)
    with pytest.raises(ToolError, match="scope"):
        await storage_node(action="rebalance", location_id=1)
    with pytest.raises(ToolError, match="scope"):
        await storage(action="move", service_id=1, node_id=1)
