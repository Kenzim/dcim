"""NFS storage API: admin nodes and services, client isolation, and the storage runner contract."""
import pytest

from app.core.config import settings
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.storage_dao import StorageDAO
from app.models.cable_run import CableRun
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.server import Server
from app.models.service import ServiceStatus
from app.models.switch_port import SwitchPort
from app.models.user import User
from app.services import private_network_service as pns

ADMIN = "/api/admin/storage"
CLIENT = "/api/client/storage"
RUNNER = "/api/runner/storage"
GIB = 1024**3


class FakeSwitch:
    def __init__(self):
        self.tagged = {}

    async def create_vlan(self, vlan_id, name):
        return None

    async def delete_vlan(self, vlan_id, name=None):
        return None

    async def get_port_vlans(self, port):
        tagged = self.tagged.get(port, set())
        return {
            "interface": port,
            "mode": "trunk" if tagged else "access",
            "native_vlan": 100,
            "allowed_all": False,
            "allowed_vlans": sorted(tagged | {100}),
        }

    async def add_port_vlan(self, port, vlan_id):
        self.tagged.setdefault(port, set()).add(vlan_id)

    async def remove_port_vlan(self, port, vlan_id):
        self.tagged.get(port, set()).discard(vlan_id)


@pytest.fixture
def world(db_session, monkeypatch, mock_redis):
    import app.core.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    switches = {}
    monkeypatch.setattr(pns, "_switch_plugin", lambda switch: switches.setdefault(switch.name, FakeSwitch()))
    location = Location(name="London", description="")
    db_session.add(location)
    db_session.commit()
    PrivateNetworkDAO.create_pool(
        db_session,
        location_id=location.id,
        vlan_start=2000,
        vlan_end=2009,
        bridge="vmbr0",
        max_networks_per_client=3,
        max_networks_per_service=4,
    )
    servers = []
    for index in range(2):
        switch = NetworkSwitch(name=f"tor-{index}", location_id=location.id, plugin_name="nxos", plugin_config={})
        server = Server(
            name=f"store-{index}", server_ip=f"10.88.0.{index + 1}", location_id=location.id,
            plugin_name="ipmi", plugin_config={},
        )
        db_session.add_all([switch, server])
        db_session.flush()
        port = NetworkPort(server_id=server.id, name="eth0", speed_mbps=25000, pxe_boot=True)
        switch_port = SwitchPort(switch_id=switch.id, name="Ethernet1/1")
        db_session.add_all([port, switch_port])
        db_session.flush()
        db_session.add(CableRun(end_a_server_port_id=port.id, end_b_switch_port_id=switch_port.id))
        servers.append(server)
    db_session.commit()

    class World:
        pass

    w = World()
    w.db, w.location, w.servers, w.switches, w.redis = db_session, location, servers, switches, mock_redis
    return w


def _login(client, username, password):
    response = client.post("/api/users/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _user(db, username):
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def admin(client, test_admin_user):
    return _login(client, test_admin_user.username, "adminpassword123")


@pytest.fixture
def alice(client, db_session):
    return _user(db_session, "alice"), None


@pytest.fixture
def bob(client, db_session):
    return _user(db_session, "bob"), None


def _headers(client, user):
    return _login(client, user.username, "secret123")


def _node(client, admin, server, **extra):
    response = client.post(f"{ADMIN}/nodes", headers=admin, json={"server_id": server.id, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _client_network(client, headers, world, name="net"):
    response = client.post(
        "/api/client/private-networks", headers=headers, json={"name": name, "location_id": world.location.id}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _storage(client, admin, owner, size_gb=100, network_id=None):
    body = {"owner_user_id": owner.id, "name": f"{owner.username}-nfs", "size_gb": size_gb}
    if network_id is not None:
        body["network_id"] = network_id
    response = client.post(f"{ADMIN}/services", headers=admin, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


# --- admin nodes ---


def test_admin_node_lifecycle_and_key_shown_once(client, world, admin):
    created = _node(client, admin, world.servers[0], name="store-a")
    assert created["api_key"].startswith("srk_")
    assert created["vlan_mode"] == "switch" and created["vlan_ports"]["ok"] is True
    assert created["vlan_ports"]["ports"][0]["switch_port"] == "Ethernet1/1"

    fetched = client.get(f"{ADMIN}/nodes/{created['id']}", headers=admin).json()
    assert "api_key" not in fetched
    assert client.get(f"{ADMIN}/nodes", headers=admin).json()[0]["name"] == "store-a"
    duplicate = client.post(f"{ADMIN}/nodes", headers=admin, json={"server_id": world.servers[0].id})
    assert duplicate.status_code == 409

    updated = client.patch(
        f"{ADMIN}/nodes/{created['id']}", headers=admin, json={"enabled": False, "vlan_mode": "preconfigured"}
    ).json()
    assert updated["enabled"] is False and updated["vlan_mode"] == "preconfigured"
    assert updated["vlan_ports"]["vlan_mode"] == "preconfigured"

    rotated = client.post(f"{ADMIN}/nodes/{created['id']}/rotate-key", headers=admin).json()
    assert rotated["api_key"] != created["api_key"]
    assert client.delete(f"{ADMIN}/nodes/{created['id']}", headers=admin).status_code == 204


def test_staff_routes_refuse_clients(client, world, alice):
    headers = _headers(client, alice[0])
    assert client.get(f"{ADMIN}/nodes", headers=headers).status_code == 403
    assert client.get(f"{ADMIN}/services", headers=headers).status_code == 403


# --- client isolation ---


def test_client_sees_and_changes_only_their_own_storage(client, world, admin, alice, bob):
    _node(client, admin, world.servers[0])
    alice_headers, bob_headers = _headers(client, alice[0]), _headers(client, bob[0])
    alice_net = _client_network(client, alice_headers, world)
    bob_net = _client_network(client, bob_headers, world, "bobnet")
    assert alice_net["storage_endpoint_ip"] == "10.0.0.254"

    alice_storage = _storage(client, admin, alice[0])
    bob_storage = _storage(client, admin, bob[0], network_id=bob_net["id"])
    assert alice_storage["status"] == "unlinked"

    # Alice cannot link to Bob's network, nor see or touch Bob's storage.
    linked = client.post(
        f"{CLIENT}/{alice_storage['service_id']}/link", headers=alice_headers, json={"network_id": bob_net["id"]}
    )
    assert linked.status_code == 404
    networks = client.get(f"{CLIENT}/{alice_storage['service_id']}/networks", headers=alice_headers).json()
    assert [n["id"] for n in networks] == [alice_net["id"]] and networks[0]["available"] is True
    linked = client.post(
        f"{CLIENT}/{alice_storage['service_id']}/link", headers=alice_headers, json={"network_id": alice_net["id"]}
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["endpoint_ip"] == "10.0.0.254"
    assert "node" not in linked.json() and "runner_state" not in linked.json()

    bob_id = bob_storage["service_id"]
    share = client.post(f"{CLIENT}/{bob_id}/shares", headers=bob_headers, json={"name": "data", "quota_gb": 10})
    assert share.status_code == 201, share.text
    share_id = share.json()["shares"][0]["id"]
    assert share.json()["shares"][0]["mount_command"] == "mount -t nfs4 10.0.0.254:/data /mnt/data"

    assert [s["service_id"] for s in client.get(CLIENT, headers=alice_headers).json()] == [
        alice_storage["service_id"]
    ]
    for method, path, body in (
        ("get", f"{CLIENT}/{bob_id}", None),
        ("get", f"{CLIENT}/{bob_id}/networks", None),
        ("post", f"{CLIENT}/{bob_id}/shares", {"name": "evil", "quota_gb": 1}),
        ("patch", f"{CLIENT}/{bob_id}/shares/{share_id}", {"quota_gb": 1}),
        ("delete", f"{CLIENT}/{bob_id}/shares/{share_id}", None),
    ):
        kwargs = {"headers": alice_headers}
        if body is not None:
            kwargs["json"] = body
        assert getattr(client, method)(path, **kwargs).status_code == 404, (method, path)
    # A share id from another storage is not found under one's own storage either.
    own = alice_storage["service_id"]
    assert client.patch(
        f"{CLIENT}/{own}/shares/{share_id}", headers=alice_headers, json={"quota_gb": 1}
    ).status_code == 404
    assert client.delete(f"{CLIENT}/{own}/shares/{share_id}", headers=alice_headers).status_code == 404
    world.db.expire_all()
    assert StorageDAO.get_share(world.db, share_id).state.value == "active"


def test_client_share_validation_and_delete(client, world, admin, alice):
    _node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    net = _client_network(client, headers, world)
    storage = _storage(client, admin, alice[0], size_gb=50, network_id=net["id"])
    sid = storage["service_id"]
    assert client.post(
        f"{CLIENT}/{sid}/shares", headers=headers, json={"name": "big", "quota_gb": 51}
    ).status_code == 409
    assert client.post(
        f"{CLIENT}/{sid}/shares", headers=headers, json={"name": "x", "quota_gb": 1, "clients": ["10.0.0.254"]}
    ).status_code == 400
    assert client.post(
        f"{CLIENT}/{sid}/shares", headers=headers, json={"name": "x", "quota_gb": 1, "clients": ["192.168.1.1"]}
    ).status_code == 400
    created = client.post(
        f"{CLIENT}/{sid}/shares",
        headers=headers,
        json={"name": "data", "quota_gb": 20, "clients": ["10.0.0.7"], "read_only": True},
    ).json()
    share = created["shares"][0]
    assert share["fstab_line"] == "10.0.0.254:/data /mnt/data nfs4 defaults,_netdev 0 0"
    assert share["status"] == "provisioning"
    deleted = client.delete(f"{CLIENT}/{sid}/shares/{share['id']}", headers=headers).json()
    assert deleted["shares"][0]["state"] == "deleting" and deleted["shares"][0]["destroy_after"]


def test_permission_preset_can_deny_share_management(client, world, admin, alice):
    from app.models.permission_set import PermissionSet
    from app.dao.service_dao import ServiceDAO

    _node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    net = _client_network(client, headers, world)
    storage = _storage(client, admin, alice[0], network_id=net["id"])
    preset = PermissionSet(name="view only storage", permissions={"storage.manage_shares": False})
    world.db.add(preset)
    world.db.commit()
    service = ServiceDAO.get_by_id(world.db, storage["service_id"])
    service.permission_set_id = preset.id
    world.db.commit()
    response = client.post(
        f"{CLIENT}/{storage['service_id']}/shares", headers=headers, json={"name": "data", "quota_gb": 1}
    )
    assert response.status_code == 403
    assert client.get(f"{CLIENT}/{storage['service_id']}", headers=headers).status_code == 200


def test_sftp_needs_its_own_permission(client, world, admin, alice):
    from app.models.permission_set import PermissionSet
    from app.dao.service_dao import ServiceDAO

    _node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    net = _client_network(client, headers, world)
    storage = _storage(client, admin, alice[0], network_id=net["id"])
    preset = PermissionSet(name="storage without sftp", permissions={"storage.sftp": False})
    world.db.add(preset)
    world.db.commit()
    service = ServiceDAO.get_by_id(world.db, storage["service_id"])
    service.permission_set_id = preset.id
    world.db.commit()
    base = f"{CLIENT}/{storage['service_id']}"

    exposed = client.post(f"{base}/shares", headers=headers, json={"name": "web", "quota_gb": 1, "sftp_access": "rw"})
    assert exposed.status_code == 403
    share = client.post(f"{base}/shares", headers=headers, json={"name": "data", "quota_gb": 1})
    assert share.status_code == 201
    opened = client.patch(f"{base}/shares/{share.json()['shares'][0]['id']}", headers=headers, json={"sftp_access": "ro"})
    assert opened.status_code == 403
    login = client.post(f"{base}/sftp-users", headers=headers, json={"name": "deploy", "generate_password": True})
    assert login.status_code == 403


def test_share_name_cannot_shadow_a_system_folder(client, world, admin, alice):
    _node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    net = _client_network(client, headers, world)
    storage = _storage(client, admin, alice[0], network_id=net["id"])
    response = client.post(
        f"{CLIENT}/{storage['service_id']}/shares", headers=headers, json={"name": "usr", "quota_gb": 1}
    )
    assert response.status_code == 400 and "reserved" in response.text


def test_network_with_storage_cannot_be_deleted(client, world, admin, alice):
    _node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    net = _client_network(client, headers, world)
    _storage(client, admin, alice[0], network_id=net["id"])
    response = client.delete(f"/api/client/private-networks/{net['id']}", headers=headers)
    assert response.status_code == 409


# --- runner ---


def test_runner_config_and_status_round_trip(client, world, admin, alice, bob):
    node_a = _node(client, admin, world.servers[0])
    node_b = _node(client, admin, world.servers[1])
    alice_headers, bob_headers = _headers(client, alice[0]), _headers(client, bob[0])
    a_net = _client_network(client, alice_headers, world)
    b_net = _client_network(client, bob_headers, world, "b")
    a_storage = _storage(client, admin, alice[0], network_id=a_net["id"])
    b_storage = _storage(client, admin, bob[0], network_id=b_net["id"])
    placed = {
        StorageDAO.get_tenant_for_service(world.db, a_storage["service_id"]).node_id,
        StorageDAO.get_tenant_for_service(world.db, b_storage["service_id"]).node_id,
    }
    assert placed == {node_a["id"], node_b["id"]}
    client.post(
        f"{CLIENT}/{a_storage['service_id']}/shares",
        headers=alice_headers,
        json={"name": "data", "quota_gb": 10, "clients": ["10.0.0.5", "10.0.0.6"]},
    )

    a_tenant = StorageDAO.get_tenant_for_service(world.db, a_storage["service_id"])
    key = node_a["api_key"] if a_tenant.node_id == node_a["id"] else node_b["api_key"]
    response = client.get(f"{RUNNER}/config", headers=_bearer(key))
    assert response.status_code == 200, response.text
    config = response.json()
    assert set(config) == {"version", "node", "settings", "sftp", "tenants"}
    assert config["sftp"]["enabled"] is False and config["sftp"]["port"] == 2222
    assert [t["id"] for t in config["tenants"]] == [a_tenant.id]
    tenant = config["tenants"][0]
    assert set(tenant) == {
        "id", "state", "terminated_at", "destroy_after", "vlan", "network_cidr", "endpoint_ip",
        "prefix_len", "size_bytes", "limits", "shares", "sftp_users",
    }
    assert tenant["sftp_users"] == []
    assert tenant["vlan"] == a_tenant.vlan and tenant["endpoint_ip"] == "10.0.0.254"
    assert tenant["shares"][0]["clients"] == ["10.0.0.5", "10.0.0.6"]
    assert set(tenant["shares"][0]) == {
        "id", "name", "quota_bytes", "clients", "read_only", "sftp", "state", "destroy_after"
    }
    assert tenant["shares"][0]["sftp"] == "off"
    share_id = tenant["shares"][0]["id"]

    status_response = client.post(
        f"{RUNNER}/status",
        headers=_bearer(key),
        json={
            "version_applied": config["version"],
            "runner_version": "0.1.0",
            "node": {"ceph_ok": True, "free_bytes": 123, "error": None},
            "tenants": [
                {"id": a_tenant.id, "state": "ready", "error": None,
                 "shares": [{"id": share_id, "state": "ready", "used_bytes": 1234, "error": None}]}
            ],
            "destroyed_tenants": [],
            "destroyed_shares": [],
            "orphans": ["stray"],
        },
    )
    assert status_response.status_code == 204, status_response.text
    view = client.get(f"{CLIENT}/{a_storage['service_id']}", headers=alice_headers).json()
    assert view["status"] == "ready" and view["shares"][0]["status"] == "ready"
    assert view["shares"][0]["used_bytes"] == 1234
    staff = client.get(f"{ADMIN}/nodes/{a_tenant.node_id}", headers=admin).json()
    assert staff["in_sync"] is True and staff["orphans"] == ["stray"] and staff["online"] is True


def test_runner_auth_failures_and_disabled_node(client, world, admin, monkeypatch):
    node = _node(client, admin, world.servers[0])
    assert client.get(f"{RUNNER}/config").status_code == 401
    assert client.get(f"{RUNNER}/config", headers=_bearer("srk_wrong")).status_code == 401
    assert client.get(f"{RUNNER}/config", headers={"Authorization": node["api_key"]}).status_code == 401

    client.patch(f"{ADMIN}/nodes/{node['id']}", headers=admin, json={"enabled": False})
    assert client.get(f"{RUNNER}/config", headers=_bearer(node["api_key"])).status_code == 401
    client.patch(f"{ADMIN}/nodes/{node['id']}", headers=admin, json={"enabled": True})
    assert client.get(f"{RUNNER}/config", headers=_bearer(node["api_key"])).status_code == 200

    monkeypatch.setattr(settings, "storage_runner_auth_failures_per_ip", 3)
    world.redis.get = lambda key: world.redis._counters.get(key)
    codes = [client.get(f"{RUNNER}/config", headers=_bearer("srk_bad")).status_code for _ in range(5)]
    assert 429 in codes
    # Once blocked, even the right key is refused from that address until the window ends.
    assert client.get(f"{RUNNER}/config", headers=_bearer(node["api_key"])).status_code == 429
    # A forged X-Forwarded-For cannot dodge the block unless the proxy header is trusted.
    spoofed = {**_bearer(node["api_key"]), "X-Forwarded-For": "203.0.113.9"}
    monkeypatch.setattr(settings, "trust_x_forwarded_for", False)
    assert client.get(f"{RUNNER}/config", headers=spoofed).status_code == 429
    monkeypatch.setattr(settings, "trust_x_forwarded_for", True)
    assert client.get(f"{RUNNER}/config", headers=spoofed).status_code == 200


def test_admin_resize_and_terminated_storage_hidden_from_client(client, world, admin, alice, db_session):
    from app.dao.service_dao import ServiceDAO
    from app.services.storage_service import StorageService

    _node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    net = _client_network(client, headers, world)
    storage = _storage(client, admin, alice[0], size_gb=20, network_id=net["id"])
    client.post(f"{CLIENT}/{storage['service_id']}/shares", headers=headers, json={"name": "d", "quota_gb": 15})
    resized = client.post(f"{ADMIN}/services/{storage['service_id']}/resize", headers=admin, json={"size_gb": 10})
    assert resized.status_code == 409
    resized = client.post(f"{ADMIN}/services/{storage['service_id']}/resize", headers=admin, json={"size_gb": 30})
    assert resized.json()["size_gb"] == 30

    service = ServiceDAO.get_by_id(db_session, storage["service_id"])
    service.status = ServiceStatus.TERMINATED
    db_session.commit()
    StorageService.mark_terminated(db_session, service.id)
    assert client.get(f"{CLIENT}/{service.id}", headers=headers).status_code == 404
    assert client.get(CLIENT, headers=headers).json() == []
    assert StorageDAO.get_tenant_for_service(db_session, service.id).destroy_after is not None
