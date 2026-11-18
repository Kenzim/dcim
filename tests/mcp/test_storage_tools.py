"""Storage MCP tools: staff node and storage tools, and the client storage tool's isolation and scopes."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.storage_dao import StorageDAO
from app.mcp.client import tools as client_tools
from app.mcp.client.context import client_mcp_auth_ctx
from app.core.client_mcp_auth import ClientMcpAuthContext
from app.models.location import Location
from app.models.server import Server
from app.models.service import ProvisioningSource, ServiceType
from app.models.user import User
from app.services.private_network_service import PrivateNetworkService
from app.services.provisioning import ProvisioningActor, ProvisioningService, ProvisionRequest
from app.services.storage_service import StorageNodeService, StorageService
from tests.conftest import TestingSessionLocal


def _user(db, username):
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _location_with_node(db, mode="preconfigured"):
    location = Location(name="mcp-storage-loc", description="")
    db.add(location)
    db.commit()
    PrivateNetworkDAO.create_pool(
        db, location_id=location.id, vlan_start=3000, vlan_end=3009, bridge="vmbr0",
        max_networks_per_client=3, max_networks_per_service=4,
    )
    server = Server(name="mcp-store", server_ip="10.70.0.1", location_id=location.id, plugin_name="ipmi", plugin_config={})
    db.add(server)
    db.commit()
    return location, server


async def _linked_storage(db, owner, location, name):
    network = await PrivateNetworkService.create_network(
        db, owner_user_id=owner.id, location_id=location.id, name=f"{name}-net", cidr=None
    )
    service = ProvisioningService.create(
        db,
        ProvisionRequest(
            name=name, service_type=ServiceType.NFS_STORAGE, owner_user_id=owner.id,
            provisioning_source=ProvisioningSource.INTERNAL, size_gb=100,
        ),
        ProvisioningActor(kind="admin", actor_id=1, name="t", source="test"),
    )
    await StorageService.link_network(db, service, network.id)
    return service


@pytest.mark.asyncio
async def test_staff_storage_node_tools(db_session, mcp_sessionlocal, mcp_auth_ctx_destructive):
    from app.mcp.tools.storage import delete_storage_node, rotate_storage_node_key, storage_node

    _, server = _location_with_node(db_session)
    created = await storage_node(action="create", server_id=server.id, vlan_mode="preconfigured")
    key = created["api_key"]
    assert key.startswith("srk_") and created["vlan_ports"]["vlan_mode"] == "preconfigured"
    listed = await storage_node()
    assert [n["id"] for n in listed["nodes"]] == [created["id"]] and "api_key" not in listed["nodes"][0]

    updated = await storage_node(action="update", node_id=created["id"], enabled=False, vlan_mode="switch")
    assert updated["enabled"] is False and updated["vlan_mode"] == "switch"
    preview = await storage_node(action="vlan_ports", server_id=server.id)
    assert preview["vlan_ports"]["ok"] is False

    with pytest.raises(ToolError, match="confirm=true"):
        await rotate_storage_node_key(created["id"])
    rotated = await rotate_storage_node_key(created["id"], confirm=True)
    db_session.expire_all()
    assert StorageDAO.get_node_by_key(db_session, key) is None
    assert StorageDAO.get_node_by_key(db_session, rotated["api_key"]).id == created["id"]

    with pytest.raises(ToolError, match="confirm=true"):
        await delete_storage_node(created["id"])
    assert (await delete_storage_node(created["id"], confirm=True))["deleted"] is True


@pytest.mark.asyncio
async def test_staff_storage_tool_shares(db_session, mcp_sessionlocal, mcp_auth_ctx_destructive):
    from app.mcp.tools.storage import storage

    location, server = _location_with_node(db_session)
    StorageNodeService.create(db_session, server_id=server.id, vlan_mode="preconfigured")
    owner = _user(db_session, "carol")
    service = await _linked_storage(db_session, owner, location, "carol-nfs")

    view = await storage(action="view", service_id=service.id)
    assert view["attachment_state"] == "active" and view["status"] == "provisioning"
    out = await storage(action="share_create", service_id=service.id, name="data", quota_gb=10, all_clients=True)
    share_id = out["shares"][0]["id"]
    out = await storage(action="share_update", service_id=service.id, share_id=share_id, quota_gb=20)
    assert out["shares"][0]["quota_gb"] == 20
    assert (await storage(action="resize", service_id=service.id, size_gb=30))["size_gb"] == 30
    with pytest.raises(ToolError, match="confirm=true"):
        await storage(action="share_delete", service_id=service.id, share_id=share_id)
    out = await storage(action="share_delete", service_id=service.id, share_id=share_id, confirm=True)
    assert out["shares"][0]["state"] == "deleting"


@pytest.mark.asyncio
async def test_staff_storage_writes_need_write_scope(mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.storage import storage, storage_node

    with pytest.raises(ToolError, match="scope"):
        await storage_node(action="create", server_id=1)
    with pytest.raises(ToolError, match="scope"):
        await storage(action="resize", service_id=1, size_gb=1)


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


@pytest.mark.asyncio
async def test_client_storage_tool_isolation_and_scopes(db_session, as_client):
    location, server = _location_with_node(db_session)
    StorageNodeService.create(db_session, server_id=server.id, vlan_mode="preconfigured")
    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    alice_storage = await _linked_storage(db_session, alice, location, "alice-nfs")
    bob_storage = await _linked_storage(db_session, bob, location, "bob-nfs")

    as_client(alice, ["read"])
    listed = await client_tools.storage()
    assert [s["service_id"] for s in listed] == [alice_storage.id]
    assert "node" not in listed[0]
    with pytest.raises(ToolError, match="not found"):
        await client_tools.storage(action="view", service_id=bob_storage.id)
    with pytest.raises(ToolError, match="scope"):
        await client_tools.storage(action="share_create", service_id=alice_storage.id, name="d", quota_gb=1)

    as_client(alice, ["read", "write"])
    out = await client_tools.storage(
        action="share_create", service_id=alice_storage.id, name="data", quota_gb=5, clients=["10.0.0.3"]
    )
    share = out["shares"][0]
    assert share["mount_command"] == "mount -t nfs4 10.0.0.254:/data /mnt/data"
    with pytest.raises(ToolError, match="not found"):
        await client_tools.storage(action="share_create", service_id=bob_storage.id, name="evil", quota_gb=1)
    with pytest.raises(ToolError, match="scope"):
        await client_tools.storage(action="share_delete", service_id=alice_storage.id, share_id=share["id"], confirm=True)

    as_client(alice, ["read", "write", "destructive"])
    with pytest.raises(ToolError, match="confirm"):
        await client_tools.storage(action="share_delete", service_id=alice_storage.id, share_id=share["id"])
    out = await client_tools.storage(
        action="share_delete", service_id=alice_storage.id, share_id=share["id"], confirm=True
    )
    assert out["shares"][0]["state"] == "deleting"
