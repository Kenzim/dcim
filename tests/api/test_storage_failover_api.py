"""Failover over HTTP: admin move, drain, rebalance and location endpoints, staff output, and the runner contract."""

import pytest

from app.models.storage import StorageNode
from tests.services.test_storage_failover import World

ADMIN = "/api/admin/storage"
RUNNER = "/api/runner/storage"


@pytest.fixture
def world(db_session, monkeypatch):
    from cryptography.fernet import Fernet

    from app.core.config import settings

    monkeypatch.setattr(settings, "service_instance_encryption_key", Fernet.generate_key().decode())
    return World(db_session)


@pytest.fixture
def admin(client, test_admin_user):
    response = client.post("/api/users/login", json={"username": test_admin_user.username, "password": "adminpassword123"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _key(world, node):
    from app.dao.storage_dao import StorageDAO

    return StorageDAO.rotate_node_key(world.db, node)


def test_node_and_tenant_output_carry_the_failover_fields(client, world, admin):
    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=b)
    node = client.get(f"{ADMIN}/nodes/{b.id}", headers=admin).json()
    assert node["draining"] is False and node["sftp_master"] is False and node["ha_active"] is True
    assert [t["tenant_id"] for t in node["serving_tenants"]] == [tenant.id]
    assert client.get(f"{ADMIN}/nodes/{a.id}", headers=admin).json()["preferred_elsewhere"][0]["tenant_id"] == tenant.id
    out = client.get(f"{ADMIN}/services/{tenant.service_id}", headers=admin).json()
    assert out["preferred_node"]["id"] == a.id and out["current_node"]["id"] == b.id
    assert out["move"] is None and "current_since" in out and "last_failover_at" in out


def test_move_endpoint(client, world, admin):
    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=a)
    url = f"{ADMIN}/services/{tenant.service_id}/move"
    out = client.post(url, headers=admin, json={"node_id": b.id, "set_preferred": False}).json()
    assert out["move"]["to_node_id"] == b.id and out["move"]["set_preferred"] is False
    assert client.post(url, headers=admin, json={"node_id": b.id}).status_code == 409
    assert client.post(f"{ADMIN}/services/99999/move", headers=admin, json={"node_id": b.id}).status_code == 404


def test_drain_endpoint_and_rebalance_endpoint(client, world, admin):
    a, b = world.node(), world.node()
    for _ in range(4):
        world.tenant(a, current=a)
    result = client.post(f"{ADMIN}/locations/{world.location.id}/rebalance", headers=admin).json()
    assert len(result["changed"]) == 2
    drained = client.post(f"{ADMIN}/nodes/{b.id}/drain", headers=admin, json={"draining": True})
    assert drained.status_code == 200 and drained.json()["draining"] is True
    assert client.post(f"{ADMIN}/nodes/{b.id}/drain", headers=admin, json={"draining": False}).json()["draining"] is False
    assert client.post(f"{ADMIN}/locations/9999/rebalance", headers=admin).status_code == 404


def test_location_endpoints_never_return_the_private_key(client, world, admin):
    world.node(sftp=True)
    url = f"{ADMIN}/locations/{world.location.id}"
    first = client.get(url, headers=admin).json()
    assert first["ha_enabled"] is True and first["sftp_host_key_set"] is False
    patched = client.patch(
        url, headers=admin,
        json={"sftp_enabled": True, "sftp_host": "files.example.com", "sftp_virtual_ip": "198.51.100.9/24", "auto_failback": False},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["sftp_host"] == "files.example.com" and body["auto_failback"] is False
    assert body["warnings"] and "outside every IPAM subnet" in body["warnings"][0]
    keyed = client.post(f"{url}/host-key", headers=admin, json={"generate": True})
    assert keyed.status_code == 200
    assert keyed.json()["sftp_host_key_fingerprint"].startswith("SHA256:")
    assert "PRIVATE KEY" not in keyed.text and "PRIVATE KEY" not in client.get(url, headers=admin).text
    assert "PRIVATE KEY" not in client.get(f"{ADMIN}/locations", headers=admin).text
    assert client.post(f"{url}/host-key", headers=admin, json={}).status_code == 400
    assert client.patch(url, headers=admin, json={"sftp_virtual_ip": "nonsense"}).status_code == 400


def test_runner_config_and_status_over_http(client, world, admin):
    a, b = world.node(sftp=True), world.node(sftp=True)
    tenant = world.tenant(a, current=a)
    client.post(f"{ADMIN}/locations/{world.location.id}/host-key", headers=admin, json={"generate": True})
    key_a, key_b = _key(world, a), _key(world, b)

    config = client.get(f"{RUNNER}/config", headers={"Authorization": f"Bearer {key_b}"}).json()
    assert [t["id"] for t in config["tenants"]] == [tenant.id] and config["sftp"]["host_key"]
    assert config["tenants"][0]["takeover_order"][0] == a.id

    status = {
        "runner_version": "0.3.0", "node": {"ceph_ok": True},
        "ha": {"lease_ok": True, "fenced": False, "sftp_master": True, "ha_active": True},
        "tenants": [{"id": tenant.id, "state": "ready", "shares": [], "serving": True, "since": "2026-11-19T10:00:00Z",
                     "move_id": None}],
        "events": [{"at": "2026-11-19T10:00:00Z", "kind": "takeover", "tenant_id": tenant.id, "from_node_id": a.id,
                    "fence": "node", "detail": "lease expired"}],
    }
    response = client.post(f"{RUNNER}/status", headers={"Authorization": f"Bearer {key_b}"}, json=status)
    assert response.status_code in (200, 204), response.text
    world.db.expire_all()
    node_b = world.db.get(StorageNode, b.id)
    assert node_b.sftp_master is True and node_b.ha_state["sftp_master"] is True
    world.db.refresh(tenant)
    assert tenant.current_node_id == b.id and tenant.last_failover_at is not None
    assert key_a != key_b
