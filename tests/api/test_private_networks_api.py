"""Private networks API: admin management and ownership on the client surface."""
import pytest

from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.models.location import Location
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.user import User
from app.services import private_network_service as pns

ADMIN = "/api/admin/private-networks"
CLIENT = "/api/client/private-networks"


class FakeProxmox:
    def __init__(self):
        self.nics = set()

    async def attach_private_nic(self, bridge, vlan_tag, *, ip_cidr=None, mac=None, vmid=None):
        self.nics.add((vmid, vlan_tag))
        return {"changed": True, "index": 1, "mac": "BC:24:11:AA:BB:CC"}

    async def detach_private_nic(self, bridge, vlan_tag, *, vmid=None):
        self.nics.discard((vmid, vlan_tag))
        return {"changed": True, "removed": [1]}


@pytest.fixture
def proxmox(monkeypatch):
    fake = FakeProxmox()

    async def resolve(db, service):
        return fake, None, "epyc", service.vm.proxmox_vmid

    monkeypatch.setattr(pns, "resolve_proxmox_plugin_for_service", resolve)
    return fake


@pytest.fixture
def location(db_session):
    row = Location(name="London", description="")
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def pool(db_session, location):
    return PrivateNetworkDAO.create_pool(
        db_session,
        location_id=location.id,
        vlan_start=2000,
        vlan_end=2009,
        bridge="vmbr0",
        max_networks_per_client=2,
        max_networks_per_service=4,
    )


def _login(client, username, password):
    response = client.post("/api/users/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture
def admin(client, test_admin_user):
    return _login(client, test_admin_user.username, "adminpassword123")


def _user(db, username):
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def alice(client, db_session):
    user = _user(db_session, "alice")
    return user, _login(client, "alice", "secret123")


@pytest.fixture
def bob(client, db_session):
    user = _user(db_session, "bob")
    return user, _login(client, "bob", "secret123")


def _vm(db, owner, location, name, vmid):
    cluster = ProxmoxInventoryDAO.create_cluster(
        db,
        name=f"{name}-cluster",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        location_id=location.id,
    )
    return ServiceDAO.create_vm(
        db,
        name=name,
        owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="epyc",
        proxmox_vmid=vmid,
    )


# --- admin ---


def test_admin_pool_lifecycle(client, admin, location):
    body = {"location_id": location.id, "vlan_start": 2000, "vlan_end": 2099, "bridge": "vmbr0"}
    created = client.post(f"{ADMIN}/pools", json=body, headers=admin)
    assert created.status_code == 201, created.text
    pool = created.json()
    assert pool["capacity"] == 100 and pool["networks_in_use"] == 0 and pool["location_name"] == "London"
    assert pool["max_networks_per_client"] == 5

    assert client.post(f"{ADMIN}/pools", json=body, headers=admin).status_code == 409
    assert client.post(f"{ADMIN}/pools", json={**body, "location_id": 9999}, headers=admin).status_code == 404

    updated = client.patch(f"{ADMIN}/pools/{pool['id']}", json={"max_networks_per_client": 9}, headers=admin)
    assert updated.status_code == 200 and updated.json()["max_networks_per_client"] == 9
    # 3968 and above are reserved by NX-OS.
    assert client.patch(f"{ADMIN}/pools/{pool['id']}", json={"vlan_end": 4000}, headers=admin).status_code == 400
    assert client.patch(f"{ADMIN}/pools/{pool['id']}", json={"bridge": "vmbr0,tag=1"}, headers=admin).status_code == 422

    assert [row["id"] for row in client.get(f"{ADMIN}/pools", headers=admin).json()] == [pool["id"]]
    assert client.delete(f"{ADMIN}/pools/{pool['id']}", headers=admin).status_code == 204
    assert client.get(f"{ADMIN}/pools", headers=admin).json() == []


def test_admin_network_lifecycle_and_attach(client, admin, db_session, location, pool, alice, bob, proxmox):
    alice_user, _ = alice
    bob_user, _ = bob
    vm = _vm(db_session, alice_user, location, "alice-vm", 101)
    bobs_vm = _vm(db_session, bob_user, location, "bob-vm", 102)

    created = client.post(
        ADMIN, json={"owner_user_id": alice_user.id, "location_id": location.id, "name": "backend"}, headers=admin
    )
    assert created.status_code == 201, created.text
    network = created.json()
    assert network["vlan_id"] == 2000 and network["cidr"] == "10.0.0.0/24"
    assert network["owner_username"] == "alice" and network["switch_synced"] is True

    attached = client.post(
        f"{ADMIN}/{network['id']}/attachments", json={"service_id": vm.id, "ip_address": "10.0.0.9"}, headers=admin
    )
    assert attached.status_code == 200, attached.text
    attachment = attached.json()["attachments"][0]
    assert attachment["state"] == "active" and attachment["ip_address"] == "10.0.0.9"
    assert attachment["prefix_length"] == 24 and attachment["vm_net_index"] == 1
    assert proxmox.nics == {(101, 2000)}

    # Staff cannot join one client's network to another client's service either.
    cross = client.post(f"{ADMIN}/{network['id']}/attachments", json={"service_id": bobs_vm.id}, headers=admin)
    assert cross.status_code == 404

    assert client.delete(f"{ADMIN}/{network['id']}", headers=admin).status_code == 409
    assert client.delete(f"{ADMIN}/pools/{pool.id}", headers=admin).status_code == 409

    listed = client.get(ADMIN, params={"owner_user_id": alice_user.id}, headers=admin).json()
    assert [row["id"] for row in listed] == [network["id"]]
    assert client.get(ADMIN, params={"owner_user_id": bob_user.id}, headers=admin).json() == []

    detached = client.delete(f"{ADMIN}/{network['id']}/attachments/{attachment['id']}", headers=admin)
    assert detached.status_code == 200 and detached.json()["attachments"] == []
    assert proxmox.nics == set()
    assert client.delete(f"{ADMIN}/{network['id']}/attachments/{attachment['id']}", headers=admin).status_code == 404

    assert client.post(f"{ADMIN}/{network['id']}/sync", headers=admin).status_code == 200
    assert client.delete(f"{ADMIN}/{network['id']}", headers=admin).status_code == 204
    assert client.get(f"{ADMIN}/{network['id']}", headers=admin).status_code == 404


def test_admin_routes_reject_clients_and_anonymous(client, alice, location, pool):
    _, headers = alice
    for method, url in [
        ("get", ADMIN),
        ("get", f"{ADMIN}/pools"),
        ("post", f"{ADMIN}/pools"),
        ("delete", f"{ADMIN}/1"),
        ("post", f"{ADMIN}/1/attachments"),
    ]:
        assert getattr(client, method)(url, headers=headers).status_code == 403, url
        assert getattr(client, method)(url).status_code in (401, 403), url


def test_cluster_location_can_be_set_and_is_listed(client, admin, location):
    created = client.post(
        "/api/proxmox/clusters",
        json={"name": "c1", "api_url": "https://pve:8006", "username": "root@pam", "password": "x"},
        headers=admin,
    )
    cluster_id = created.json()["id"]
    assert client.get("/api/proxmox/clusters", headers=admin).json()[0]["location_id"] is None

    assert (
        client.put(f"/api/proxmox/clusters/{cluster_id}", json={"location_id": 9999}, headers=admin).status_code == 404
    )
    updated = client.put(f"/api/proxmox/clusters/{cluster_id}", json={"location_id": location.id}, headers=admin)
    assert updated.status_code == 200
    assert client.get("/api/proxmox/clusters", headers=admin).json()[0]["location_id"] == location.id


# --- client ---


def test_client_creates_attaches_detaches_and_deletes(client, db_session, location, pool, alice, proxmox):
    user, headers = alice
    vm = _vm(db_session, user, location, "alice-vm", 101)

    options = client.get(f"{CLIENT}/options", headers=headers).json()
    assert options["default_cidr"] == "10.0.0.0/24"
    assert options["locations"] == [
        {
            "location_id": location.id,
            "location_name": "London",
            "max_networks": 2,
            "networks_in_use": 0,
            "max_networks_per_service": 4,
        }
    ]
    assert [row["service_id"] for row in options["services"]] == [vm.id]

    created = client.post(CLIENT, json={"location_id": location.id, "name": "backend"}, headers=headers)
    assert created.status_code == 201, created.text
    network = created.json()
    # Staff-only fields are not exposed to clients.
    for hidden in ("owner_user_id", "owner_username", "switch_synced", "last_error"):
        assert network[hidden] is None

    attached = client.post(f"{CLIENT}/{network['id']}/attachments", json={"service_id": vm.id}, headers=headers)
    assert attached.status_code == 200, attached.text
    attachment = attached.json()["attachments"][0]
    assert attachment["ip_address"] == "10.0.0.1" and attachment["service_name"] == "alice-vm"
    assert attachment["server_id"] is None and attachment["vm_net_index"] is None
    assert proxmox.nics == {(101, 2000)}

    assert client.delete(f"{CLIENT}/{network['id']}", headers=headers).status_code == 409
    detached = client.delete(f"{CLIENT}/{network['id']}/attachments/{attachment['id']}", headers=headers)
    assert detached.status_code == 200 and detached.json()["attachments"] == []
    assert client.delete(f"{CLIENT}/{network['id']}", headers=headers).status_code == 204
    assert client.get(CLIENT, headers=headers).json() == []


def test_client_limit_and_validation(client, location, pool, alice):
    _, headers = alice
    for name in ("one", "two"):
        assert client.post(CLIENT, json={"location_id": location.id, "name": name}, headers=headers).status_code == 201
    over = client.post(CLIENT, json={"location_id": location.id, "name": "three"}, headers=headers)
    assert over.status_code == 409 and "Limit of 2" in over.json()["detail"]

    bad_range = client.post(
        CLIENT, json={"location_id": location.id, "name": "x", "cidr": "8.8.8.0/24"}, headers=headers
    )
    assert bad_range.status_code == 400
    assert client.post(CLIENT, json={"location_id": location.id, "name": ""}, headers=headers).status_code == 422
    assert client.post(CLIENT, json={"location_id": 9999, "name": "x"}, headers=headers).status_code == 409


def test_client_cannot_see_or_change_another_clients_network(client, db_session, location, pool, alice, bob, proxmox):
    alice_user, alice_headers = alice
    bob_user, bob_headers = bob
    alice_vm = _vm(db_session, alice_user, location, "alice-vm", 101)
    bob_vm = _vm(db_session, bob_user, location, "bob-vm", 102)
    network = client.post(CLIENT, json={"location_id": location.id, "name": "backend"}, headers=alice_headers).json()
    attachment = client.post(
        f"{CLIENT}/{network['id']}/attachments", json={"service_id": alice_vm.id}, headers=alice_headers
    ).json()["attachments"][0]

    # Bob cannot read, join, detach from or delete Alice's network; it looks like it does not exist.
    assert client.get(CLIENT, headers=bob_headers).json() == []
    assert client.get(f"{CLIENT}/{network['id']}", headers=bob_headers).status_code == 404
    assert (
        client.post(
            f"{CLIENT}/{network['id']}/attachments", json={"service_id": bob_vm.id}, headers=bob_headers
        ).status_code
        == 404
    )
    assert (
        client.delete(f"{CLIENT}/{network['id']}/attachments/{attachment['id']}", headers=bob_headers).status_code
        == 404
    )
    assert client.delete(f"{CLIENT}/{network['id']}", headers=bob_headers).status_code == 404

    # Alice cannot pull Bob's VM into her network.
    stolen = client.post(f"{CLIENT}/{network['id']}/attachments", json={"service_id": bob_vm.id}, headers=alice_headers)
    assert stolen.status_code == 404

    # Bob cannot detach Alice's attachment by going through a network of his own.
    bobs = client.post(CLIENT, json={"location_id": location.id, "name": "mine"}, headers=bob_headers).json()
    assert (
        client.delete(f"{CLIENT}/{bobs['id']}/attachments/{attachment['id']}", headers=bob_headers).status_code == 404
    )

    assert proxmox.nics == {(101, 2000)}
    still = client.get(f"{CLIENT}/{network['id']}", headers=alice_headers).json()
    assert [row["id"] for row in still["attachments"]] == [attachment["id"]]
    assert [row["service_id"] for row in client.get(f"{CLIENT}/options", headers=bob_headers).json()["services"]] == [
        bob_vm.id
    ]


def test_client_routes_reject_staff_and_anonymous(client, admin, location, pool):
    assert client.get(CLIENT, headers=admin).status_code == 403
    assert client.post(CLIENT, json={"location_id": location.id, "name": "x"}, headers=admin).status_code == 403
    assert client.get(CLIENT).status_code in (401, 403)
    assert client.get(f"{CLIENT}/options").status_code in (401, 403)
