"""NFS storage as a product: storage-only clients, retention without servers, and SFTP access."""

import base64
import hashlib

import bcrypt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.service_dao import ServiceDAO
from app.dao.storage_dao import StorageDAO
from app.models.cable_run import CableRun
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.private_network import PrivateNetworkStatus
from app.models.server import Server
from app.models.service import ServiceStatus
from app.models.storage import StorageSftpUser
from app.models.switch_port import SwitchPort
from app.services import storage_sftp
from app.services.private_network_service import PrivateNetworkService
from app.services.service_lifecycle import ServiceLifecycle
from app.services.storage_service import StorageService
from tests.api.test_storage_api import (  # noqa: F401  (fixtures)
    ADMIN,
    CLIENT,
    RUNNER,
    _bearer,
    _client_network,
    _headers,
    _node,
    _storage,
    admin,
    alice,
    bob,
    world,
)


def _ed25519_line(comment="laptop"):
    key = ed25519.Ed25519PrivateKey.generate().public_key()
    raw = key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    return f"{raw} {comment}"


def _rsa_line(bits):
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits).public_key()
    return key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


def _sftp_node(client, admin, server, host="sftp1.example.net"):
    node = _node(client, admin, server)
    response = client.patch(
        f"{ADMIN}/nodes/{node['id']}", headers=admin, json={"sftp_enabled": True, "sftp_host": host}
    )
    assert response.status_code == 200, response.text
    return node


def _runner_config(client, key):
    response = client.get(f"{RUNNER}/config", headers=_bearer(key))
    assert response.status_code == 200, response.text
    return response.json()


# --- credentials ---


def test_public_key_validation_and_fingerprint():
    line = _ed25519_line("my laptop")
    parsed = storage_sftp.parse_public_key(line)
    blob = base64.b64decode(line.split()[1])
    assert parsed["fingerprint"] == "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert parsed["type"] == "ssh-ed25519" and parsed["comment"] == "my laptop"
    assert storage_sftp.parse_public_key(_rsa_line(2048))["type"] == "ssh-rsa"

    for bad in (
        _rsa_line(1024),
        "ssh-dss AAAAB3NzaC1kc3MAAACBAP",
        "ssh-ed25519 not-base64!",
        "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x01x").decode(),
        "just-one-field",
        "",
    ):
        with pytest.raises(storage_sftp.SftpError):
            storage_sftp.parse_public_key(bad)

    # Duplicates are dropped, and comments cannot carry control characters.
    keys = storage_sftp.parse_public_keys([line, line, "", line.replace("my laptop", "a\nb")])
    assert len(keys) == 1
    with pytest.raises(storage_sftp.SftpError):
        storage_sftp.parse_public_keys([_ed25519_line() for _ in range(storage_sftp.MAX_KEYS_PER_USER + 1)])


def test_password_hash_and_allow_list_rules():
    hashed = storage_sftp.hash_password("correct horse battery")
    assert hashed.startswith("$2b$12$")
    assert bcrypt.checkpw(b"correct horse battery", hashed.encode())
    assert "correct horse" not in hashed
    for bad in ("short", "x" * 73):
        with pytest.raises(storage_sftp.SftpError):
            storage_sftp.hash_password(bad)
    assert len(storage_sftp.generate_password()) >= storage_sftp.MIN_PASSWORD_LENGTH

    assert storage_sftp.parse_allowed_ips(["203.0.113.7", "198.51.100.0/24", "2001:db8::/32", "203.0.113.7"]) == [
        "203.0.113.7/32", "198.51.100.0/24", "2001:db8::/32",
    ]
    with pytest.raises(storage_sftp.SftpError):
        storage_sftp.parse_allowed_ips(["not-an-ip"])


# --- SFTP users through the client API ---


def test_client_sftp_users_store_only_hashes_and_runner_gets_no_plaintext(client, world, admin, alice):
    node = _sftp_node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    network = _client_network(client, headers, world)
    storage = _storage(client, admin, alice[0], network_id=network["id"])
    sid = storage["service_id"]
    share = client.post(
        f"{CLIENT}/{sid}/shares", headers=headers, json={"name": "data", "quota_gb": 10, "sftp_access": "ro"}
    )
    assert share.status_code == 201, share.text
    assert share.json()["shares"][0]["sftp_access"] == "ro"

    key = _ed25519_line()
    created = client.post(
        f"{CLIENT}/{sid}/sftp-users",
        headers=headers,
        json={"name": "backup", "public_keys": [key], "generate_password": True, "allowed_ips": ["203.0.113.0/24"]},
    )
    assert created.status_code == 201, created.text
    generated = created.json()["generated_password"]
    user = created.json()["sftp"]["users"][0]
    assert user["login"] == f"backup.{sid}" and user["has_password"] is True
    assert user["sftp_command"] == f"sftp -P 2222 backup.{sid}@sftp1.example.net"
    assert created.json()["sftp"]["state"] == "provisioning"
    assert created.json()["sftp"]["shares"] == [{"name": "data", "access": "ro", "path": "/data"}]

    # The password is shown once, and only its hash is stored.
    view = client.get(f"{CLIENT}/{sid}", headers=headers)
    assert generated not in view.text and "password_hash" not in view.text and "generated_password" not in view.json()
    row = world.db.query(StorageSftpUser).one()
    assert row.password_hash != generated and bcrypt.checkpw(generated.encode(), row.password_hash.encode())

    config = _runner_config(client, node["api_key"])
    assert config["sftp"] == {
        "enabled": True, "port": 2222, "max_auth_failures": 10, "failure_window_seconds": 600, "ban_seconds": 900,
    }
    tenant = config["tenants"][0]
    assert tenant["shares"][0]["sftp"] == "ro"
    assert tenant["sftp_users"] == [
        {
            "id": row.id,
            "username": f"backup.{sid}",
            "public_keys": [key],
            "password_hash": row.password_hash,
            "allowed_ips": ["203.0.113.0/24"],
        }
    ]
    assert generated not in str(config)

    # Change: clear the password (the key remains), then the last credential cannot go.
    updated = client.patch(f"{CLIENT}/{sid}/sftp-users/{row.id}", headers=headers, json={"clear_password": True})
    assert updated.status_code == 200 and updated.json()["sftp"]["users"][0]["has_password"] is False
    emptied = client.patch(f"{CLIENT}/{sid}/sftp-users/{row.id}", headers=headers, json={"public_keys": []})
    assert emptied.status_code == 400
    disabled = client.patch(f"{CLIENT}/{sid}/sftp-users/{row.id}", headers=headers, json={"enabled": False})
    assert disabled.json()["sftp"]["state"] == "off"
    assert _runner_config(client, node["api_key"])["tenants"][0]["sftp_users"] == []

    for body in (
        {"name": "Bad Name", "public_keys": [key]},
        {"name": "nocreds"},
        {"name": "weak", "password": "short"},
        {"name": "rsa", "public_keys": [_rsa_line(1024)]},
    ):
        assert client.post(f"{CLIENT}/{sid}/sftp-users", headers=headers, json=body).status_code == 400, body
    assert client.post(
        f"{CLIENT}/{sid}/sftp-users", headers=headers, json={"name": "backup", "public_keys": [key]}
    ).status_code == 409
    assert client.patch(
        f"{CLIENT}/{sid}/shares/{share.json()['shares'][0]['id']}", headers=headers, json={"sftp_access": "all"}
    ).status_code == 422

    assert client.delete(f"{CLIENT}/{sid}/sftp-users/{row.id}", headers=headers).status_code == 200
    assert world.db.query(StorageSftpUser).count() == 0


def test_sftp_users_are_isolated_between_tenants_and_nodes(client, world, admin, alice, bob):
    node_a = _sftp_node(client, admin, world.servers[0])
    node_b = _sftp_node(client, admin, world.servers[1], host="sftp2.example.net")
    alice_headers, bob_headers = _headers(client, alice[0]), _headers(client, bob[0])
    a_storage = _storage(client, admin, alice[0], network_id=_client_network(client, alice_headers, world)["id"])
    b_storage = _storage(client, admin, bob[0], network_id=_client_network(client, bob_headers, world, "b")["id"])
    a_id, b_id = a_storage["service_id"], b_storage["service_id"]
    for sid, headers in ((a_id, alice_headers), (b_id, bob_headers)):
        client.post(f"{CLIENT}/{sid}/shares", headers=headers, json={"name": "data", "quota_gb": 5, "sftp_access": "rw"})
    a_user = client.post(
        f"{CLIENT}/{a_id}/sftp-users", headers=alice_headers, json={"name": "web", "public_keys": [_ed25519_line()]}
    ).json()["sftp"]["users"][0]
    b_user = client.post(
        f"{CLIENT}/{b_id}/sftp-users", headers=bob_headers, json={"name": "web", "public_keys": [_ed25519_line()]}
    ).json()["sftp"]["users"][0]
    # The same short name gives different logins, so the front end can never confuse them.
    assert a_user["login"] != b_user["login"]

    # Bob cannot see, add, change or delete Alice's SFTP users.
    for method, path, body in (
        ("post", f"{CLIENT}/{a_id}/sftp-users", {"name": "evil", "public_keys": [_ed25519_line()]}),
        ("patch", f"{CLIENT}/{a_id}/sftp-users/{a_user['id']}", {"public_keys": [_ed25519_line()]}),
        ("delete", f"{CLIENT}/{a_id}/sftp-users/{a_user['id']}", None),
        # Nor reach Alice's user through his own storage.
        ("patch", f"{CLIENT}/{b_id}/sftp-users/{a_user['id']}", {"enabled": False}),
        ("delete", f"{CLIENT}/{b_id}/sftp-users/{a_user['id']}", None),
    ):
        kwargs = {"headers": bob_headers}
        if body is not None:
            kwargs["json"] = body
        assert getattr(client, method)(path, **kwargs).status_code == 404, path

    # Each node's runner only receives the logins of the tenants placed on it.
    served = {}
    for node in (node_a, node_b):
        for tenant in _runner_config(client, node["api_key"])["tenants"]:
            served.setdefault(node["id"], []).extend(u["username"] for u in tenant["sftp_users"])
    a_node = StorageDAO.get_tenant_for_service(world.db, a_id).node_id
    b_node = StorageDAO.get_tenant_for_service(world.db, b_id).node_id
    assert a_node != b_node
    assert served[a_node] == [a_user["login"]] and served[b_node] == [b_user["login"]]

    # A runner cannot record a login for a user on another node.
    a_key = node_a["api_key"] if a_node == node_a["id"] else node_b["api_key"]
    report = {
        "version_applied": "x", "runner_version": "0.2.0", "node": {"ceph_ok": True},
        "tenants": [{"id": StorageDAO.get_tenant_for_service(world.db, a_id).id, "sftp": {"state": "ready"}}],
        "destroyed_tenants": [], "destroyed_shares": [], "orphans": [],
        "sftp": {"listening": True, "port": 2222, "host_key": "ssh-ed25519 AAAA", "host_key_fingerprint": "SHA256:abc"},
        "sftp_logins": [
            {"user_id": a_user["id"], "at": "2026-11-18T10:00:00Z", "ip": "203.0.113.5"},
            {"user_id": b_user["id"], "at": "2026-11-18T10:00:00Z", "ip": "203.0.113.6"},
        ],
    }
    assert client.post(f"{RUNNER}/status", headers=_bearer(a_key), json=report).status_code == 204
    a_view = client.get(f"{CLIENT}/{a_id}", headers=alice_headers).json()["sftp"]
    b_view = client.get(f"{CLIENT}/{b_id}", headers=bob_headers).json()["sftp"]
    assert a_view["users"][0]["last_login_ip"] == "203.0.113.5" and a_view["host_key_fingerprint"] == "SHA256:abc"
    assert a_view["state"] == "ready"
    assert b_view["users"][0]["last_login_at"] is None


def test_suspend_stops_sftp_and_terminate_removes_it(client, world, admin, alice):
    node = _sftp_node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    storage = _storage(client, admin, alice[0], network_id=_client_network(client, headers, world)["id"])
    sid = storage["service_id"]
    client.post(f"{CLIENT}/{sid}/shares", headers=headers, json={"name": "data", "quota_gb": 5, "sftp_access": "rw"})
    client.post(f"{CLIENT}/{sid}/sftp-users", headers=headers, json={"name": "web", "public_keys": [_ed25519_line()]})
    assert len(_runner_config(client, node["api_key"])["tenants"][0]["sftp_users"]) == 1

    service = ServiceDAO.get_by_id(world.db, sid)
    service.status = ServiceStatus.SUSPENDED
    world.db.commit()
    tenant = _runner_config(client, node["api_key"])["tenants"][0]
    assert tenant["state"] == "suspended" and tenant["sftp_users"] == []
    assert client.get(f"{CLIENT}/{sid}", headers=headers).json()["sftp"]["state"] == "suspended"
    refused = client.post(
        f"{CLIENT}/{sid}/sftp-users", headers=headers, json={"name": "late", "public_keys": [_ed25519_line()]}
    )
    assert refused.status_code == 409

    service.status = ServiceStatus.ACTIVE
    world.db.commit()
    assert len(_runner_config(client, node["api_key"])["tenants"][0]["sftp_users"]) == 1

    # Turning SFTP off on the node stops serving every login at once.
    client.patch(f"{ADMIN}/nodes/{node['id']}", headers=admin, json={"sftp_enabled": False})
    config = _runner_config(client, node["api_key"])
    assert config["sftp"]["enabled"] is False and config["tenants"][0]["sftp_users"] == []
    client.patch(f"{ADMIN}/nodes/{node['id']}", headers=admin, json={"sftp_enabled": True})

    StorageService.mark_terminated(world.db, sid)
    service.status = ServiceStatus.TERMINATED
    world.db.commit()
    tenant = _runner_config(client, node["api_key"])["tenants"][0]
    assert tenant["state"] == "terminated" and tenant["sftp_users"] == []


def test_node_sftp_settings_validation(client, world, admin):
    node = _node(client, admin, world.servers[0])
    url = f"{ADMIN}/nodes/{node['id']}"
    assert client.patch(url, headers=admin, json={"sftp_enabled": True}).status_code == 400
    assert client.patch(url, headers=admin, json={"sftp_host": "bad host!"}).status_code == 400
    assert client.patch(url, headers=admin, json={"sftp_host": "a.example", "sftp_port": 2049}).status_code == 400
    ok = client.patch(url, headers=admin, json={"sftp_host": "A.Example.", "sftp_port": 22022, "sftp_enabled": True})
    assert ok.status_code == 200, ok.text
    assert ok.json()["sftp_host"] == "a.example" and ok.json()["sftp_port"] == 22022
    assert client.patch(url, headers=admin, json={"sftp_host": ""}).status_code == 400


# --- storage-only clients ---


def test_storage_only_client_orders_links_and_uses_storage(client, world, admin, alice):
    _node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    other = Location(name="Paris", description="")
    world.db.add(other)
    world.db.commit()

    # No storage node in Paris: refused at order.
    refused = client.post(
        f"{ADMIN}/services", headers=admin,
        json={"owner_user_id": alice[0].id, "name": "x", "size_gb": 10, "location_id": other.id},
    )
    assert refused.status_code == 400

    created = client.post(
        f"{ADMIN}/services", headers=admin,
        json={"owner_user_id": alice[0].id, "name": "alice-nfs", "size_gb": 100, "location_id": world.location.id},
    )
    assert created.status_code == 201, created.text
    sid = created.json()["service_id"]
    assert created.json()["location_name"] == "London" and created.json()["status"] == "unlinked"

    # Alice has no servers and no network, yet sees her storage and can link it.
    assert PrivateNetworkDAO.list_networks(world.db, owner_user_id=alice[0].id) == []
    assert [s["service_id"] for s in client.get(CLIENT, headers=headers).json()] == [sid]
    assert client.get(f"{CLIENT}/{sid}/networks", headers=headers).json() == []
    linked = client.post(f"{CLIENT}/{sid}/link", headers=headers, json={"network_name": "files"})
    assert linked.status_code == 200, linked.text
    assert linked.json()["network"]["name"] == "files" and linked.json()["endpoint_ip"] == "10.0.0.254"
    network = PrivateNetworkDAO.list_networks(world.db, owner_user_id=alice[0].id)[0]
    assert network.location_id == world.location.id

    share = client.post(f"{CLIENT}/{sid}/shares", headers=headers, json={"name": "data", "quota_gb": 10})
    assert share.status_code == 201, share.text
    assert share.json()["shares"][0]["mount_command"] == "mount -t nfs4 10.0.0.254:/data /mnt/data"


def test_storage_only_client_can_create_a_network_in_the_portal(client, world, admin, alice):
    _node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    assert client.get("/api/client/private-networks/options", headers=headers).json()["locations"] == []
    client.post(
        f"{ADMIN}/services", headers=admin,
        json={"owner_user_id": alice[0].id, "name": "nfs", "size_gb": 10, "location_id": world.location.id},
    )
    options = client.get("/api/client/private-networks/options", headers=headers).json()
    assert [loc["location_id"] for loc in options["locations"]] == [world.location.id]
    assert options["services"] == []
    created = client.post(
        "/api/client/private-networks", headers=headers, json={"location_id": world.location.id, "name": "files"}
    )
    assert created.status_code == 201, created.text


def test_link_without_network_needs_a_location_when_several_offer_storage(client, world, admin, alice, db_session):
    _node(client, admin, world.servers[0])
    paris = Location(name="Paris", description="")
    db_session.add(paris)
    db_session.flush()
    server = Server(name="store-p", server_ip="10.88.1.1", location_id=paris.id, plugin_name="ipmi", plugin_config={})
    db_session.add(server)
    db_session.commit()
    _node(client, admin, server, vlan_mode="preconfigured")

    headers = _headers(client, alice[0])
    sid = _storage(client, admin, alice[0])["service_id"]
    assert client.post(f"{CLIENT}/{sid}/link", headers=headers, json={}).status_code == 400
    # Paris has storage but no private network pool, so a network cannot be created there.
    assert client.post(f"{CLIENT}/{sid}/link", headers=headers, json={"location_id": paris.id}).status_code in (400, 409)
    linked = client.post(f"{CLIENT}/{sid}/link", headers=headers, json={"location_id": world.location.id})
    assert linked.status_code == 200, linked.text
    # A failed attempt leaves nothing half linked.
    assert len(PrivateNetworkDAO.list_networks(db_session, owner_user_id=alice[0].id)) == 1


def test_storage_ordered_for_a_location_only_links_there(client, world, admin, alice, db_session):
    _node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    network = _client_network(client, headers, world)
    paris = Location(name="Paris", description="")
    db_session.add(paris)
    db_session.flush()
    server = Server(name="store-p", server_ip="10.88.1.1", location_id=paris.id, plugin_name="ipmi", plugin_config={})
    db_session.add(server)
    db_session.commit()
    _node(client, admin, server, vlan_mode="preconfigured")
    created = client.post(
        f"{ADMIN}/services", headers=admin,
        json={"owner_user_id": alice[0].id, "name": "p", "size_gb": 5, "location_id": paris.id},
    ).json()
    networks = client.get(f"{CLIENT}/{created['service_id']}/networks", headers=headers).json()
    assert networks[0]["available"] is False and "another location" in networks[0]["reason"]
    linked = client.post(f"{CLIENT}/{created['service_id']}/link", headers=headers, json={"network_id": network["id"]})
    assert linked.status_code == 409


# --- billing (WHMCS) ---


@pytest.mark.asyncio
async def test_whmcs_order_with_size_and_location_and_client_area_summary(client, world, admin, alice):
    from app.dao.billing_integration_dao import BillingIntegrationDAO
    from tests.api.test_billing_storage import _product

    _sftp_node(client, admin, world.servers[0])
    _product(client)
    integration = BillingIntegrationDAO.create(world.db, name="whmcs-sftp", integration_type="whmcs")
    billing = {"Authorization": f"Bearer {integration.plaintext_api_key}"}
    body = {
        "name": "whmcs-nfs",
        "external_user_id": "whmcs-client-1",
        "external_username": "storageonly",
        "external_email": "storageonly@example.com",
        "service_type": "nfs_storage",
        "product_code": "nfs-50",
        "service_config": {"size_gb": 250, "location_id": 999},
    }
    assert client.post("/api/billing/bare-metal/services", headers=billing, json=body).status_code == 400
    body["service_config"]["location_id"] = world.location.id
    created = client.post("/api/billing/bare-metal/services", headers=billing, json=body)
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    tenant = StorageDAO.get_tenant_for_service(world.db, sid)
    assert tenant.size_bytes == 250 * 1024**3 and tenant.location_id == world.location.id

    summary = client.get(f"/api/billing/services/{sid}/storage", headers=billing).json()
    assert summary["location_name"] == "London" and summary["shares"] == [] and summary["linked"] is False
    # Not placed on a node until it is linked, so there is no SFTP endpoint yet.
    assert summary["sftp"]["available"] is False and summary["sftp"]["host"] is None

    owner = ServiceDAO.get_by_id(world.db, sid).owner_user_id
    service = ServiceDAO.get_by_id(world.db, sid)
    await StorageService.link_network(world.db, service, None)
    tenant = StorageDAO.get_tenant_for_service(world.db, sid)
    StorageService.create_share(world.db, tenant, name="data", quota_gb=10, sftp_access="rw")
    storage_sftp.create_user(world.db, tenant, name="web", public_keys=[_ed25519_line()])
    summary = client.get(f"/api/billing/services/{sid}/storage", headers=billing).json()
    assert summary["linked"] is True and summary["network_name"] == f"storage-{sid}"
    assert summary["sftp"]["available"] is True and summary["sftp"]["host"] == "sftp1.example.net"
    share = summary["shares"][0]
    assert share["name"] == "data" and share["sftp_access"] == "rw" and share["quota_gb"] == 10
    assert share["mount_command"] == "mount -t nfs4 10.0.0.254:/data /mnt/data"
    assert summary["sftp"]["users"] == [
        {"login": f"web.{sid}", "enabled": True, "sftp_command": f"sftp -P 2222 web.{sid}@sftp1.example.net"}
    ]
    assert PrivateNetworkDAO.list_networks(world.db, owner_user_id=owner)[0].location_id == world.location.id


# --- retention: servers come and go, storage stays ---


def _dedicated_server(db, world, owner, name):
    switch = NetworkSwitch(name=f"tor-{name}", location_id=world.location.id, plugin_name="nxos", plugin_config={})
    server = Server(
        name=name, server_ip="10.88.2.1", location_id=world.location.id, plugin_name="ipmi", plugin_config={}
    )
    db.add_all([switch, server])
    db.flush()
    port = NetworkPort(server_id=server.id, name="eth0", speed_mbps=10000, pxe_boot=True)
    switch_port = SwitchPort(switch_id=switch.id, name="Ethernet1/9")
    db.add_all([port, switch_port])
    db.flush()
    db.add(CableRun(end_a_server_port_id=port.id, end_b_switch_port_id=switch_port.id))
    db.commit()
    return ServiceDAO.create_bare_metal(
        db, name=f"{name}-svc", server_id=server.id, owner_user_id=owner.id, status=ServiceStatus.ACTIVE
    )


@pytest.mark.asyncio
async def test_terminating_suspending_all_servers_keeps_storage_and_its_network(
    client, world, admin, alice, monkeypatch
):
    node = _node(client, admin, world.servers[0])
    headers = _headers(client, alice[0])
    network = _client_network(client, headers, world)
    sid = _storage(client, admin, alice[0], network_id=network["id"])["service_id"]
    servers = [_dedicated_server(world.db, world, alice[0], name) for name in ("bm-a", "bm-b")]
    for service in servers:
        await PrivateNetworkService.attach(world.db, network["id"], service.id)

    def storage_state():
        world.db.expire_all()
        tenant = StorageDAO.get_tenant_for_service(world.db, sid)
        net = PrivateNetworkDAO.get_network(world.db, network["id"])
        attachments = {a.service_id: (a.state.value, a.server_id, a.ip_address) for a in net.attachments}
        return tenant.network_id, tenant.endpoint_ip, tenant.terminated_at, net.status, attachments.get(sid)

    before = storage_state()
    assert before[3] == PrivateNetworkStatus.ACTIVE and before[4] is not None
    assert before[4][1] == world.servers[0].id and before[4][2] == "10.0.0.254"
    storage_vlans = set(world.switches["tor-0"].tagged.get("Ethernet1/1", set()))

    # Suspend every server (non-payment), as billing does.
    async def no_power(self, db, service):
        return None

    monkeypatch.setattr(ServiceLifecycle, "ensure_powered_off", no_power)
    for service in servers:
        await ServiceLifecycle().suspend(world.db, service, reason="nonpayment")
    world.db.commit()
    await PrivateNetworkService.reconcile(world.db)
    StorageService.reconcile_terminations(world.db)
    assert storage_state() == before

    # Terminate every server, as the billing terminate (or an expiry) does.
    for service in servers:
        service.status = ServiceStatus.TERMINATED
        world.db.commit()
        StorageService.mark_terminated_quietly(world.db, service.id)
        await PrivateNetworkService.release_service_quietly(world.db, service.id)
    await PrivateNetworkService.reconcile(world.db)
    StorageService.reconcile_terminations(world.db)
    after = storage_state()
    assert after == before
    net = PrivateNetworkDAO.get_network(world.db, network["id"])
    assert [a.service_id for a in net.attachments] == [sid]
    # The storage node's switch port still carries the network.
    assert world.switches["tor-0"].tagged.get("Ethernet1/1", set()) == storage_vlans
    assert network_vlan(world, network["id"]) in storage_vlans

    # The client still sees and manages their storage with no servers left.
    view = client.get(f"{CLIENT}/{sid}", headers=headers)
    assert view.status_code == 200 and view.json()["network"]["id"] == network["id"]
    assert client.post(f"{CLIENT}/{sid}/shares", headers=headers, json={"name": "kept", "quota_gb": 1}).status_code == 201
    config = client.get(f"{RUNNER}/config", headers=_bearer(node["api_key"])).json()
    assert config["tenants"][0]["state"] == "active"


def network_vlan(world, network_id):
    return PrivateNetworkDAO.get_network(world.db, network_id).segment_id
