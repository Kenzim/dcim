"""Proxy fast path over HTTP: runner placement, subnet pinning, runner config and the client surface."""
import pytest

from app.dao.ipam_dao import IPAMDAO
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.proxy_runner_dao import ProxyRunnerDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.models.location import Location
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.models.user import User

RUNNERS = "/api/admin/proxy-runners"
CLIENT = "/api/client/private-networks"


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
def location(db_session):
    row = Location(name="London", description="")
    db_session.add(row)
    db_session.commit()
    pool = PrivateNetworkDAO.create_pool(
        db_session, location_id=row.id, vlan_start=2000, vlan_end=2009, bridge="vmbr0",
        max_networks_per_client=2, max_networks_per_service=4,
    )
    pool.proxy_access_enabled = True
    db_session.commit()
    return row


def _runner(db, name):
    row, _ = ProxyRunnerDAO.create(db, name=name, api_key=f"key-{name}")
    return row


def _config(client, name):
    response = client.get("/api/runner/proxy/config", headers={"Authorization": f"Bearer key-{name}"})
    assert response.status_code == 200, response.text
    return response.json()


def _service(db, owner, location, name, service_type):
    server = ServerDAO.create(
        db, name=f"{name}-srv", server_ip="10.60.0.1", location_id=location.id, plugin_name="ipmi", plugin_config={}
    )
    return ServiceDAO.create_bare_metal(
        db,
        name=name,
        server_id=server.id,
        owner_user_id=owner.id,
        service_type=service_type,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.INTERNAL,
    )


def _proxy(db, owner, location, subnet, name, username):
    service = _service(db, owner, location, name, ServiceType.HTTP_PROXY)
    return service, IPAMDAO.assign_ip(db, service.id, subnet_id=subnet.id, username=username, password="secret-pw-1")


def test_unplaced_runner_config_is_unchanged(client, db_session, location):
    _runner(db_session, "plain")
    subnet = IPAMDAO.create_subnet(db_session, name="a", cidr="203.0.113.0/29", location_id=location.id)
    _proxy(db_session, _user(db_session, "alice"), location, subnet, "px", "alice1")
    body = _config(client, "plain")
    assert set(body) == {"version", "runner_id", "assignments"}
    assert [a["username"] for a in body["assignments"]] == ["alice1"]


def test_admin_places_runner_pins_subnet_and_runner_gets_fast_path(client, admin, db_session, location):
    runner = _runner(db_session, "r1")
    plain_version = _config(client, "r1")["version"]

    placed = client.patch(f"{RUNNERS}/{runner.id}", json={"location_id": location.id}, headers=admin)
    assert placed.status_code == 200, placed.text
    assert placed.json()["location_name"] == "London" and placed.json()["vlan_mode"] == "preconfigured"
    bad = client.patch(f"{RUNNERS}/{runner.id}", json={"vlan_mode": "switch"}, headers=admin)
    assert bad.status_code == 400

    subnet = IPAMDAO.create_subnet(db_session, name="a", cidr="203.0.113.0/29", location_id=location.id)
    pinned = client.patch(f"/api/ipam/subnets/{subnet.id}", json={"proxy_runner_id": runner.id}, headers=admin)
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["fast_path"]["ip"] == "100.80.0.1"
    assert pinned.json()["proxy_runner_name"] == "r1"
    listed = client.get(f"{RUNNERS}/{runner.id}", headers=admin).json()
    assert listed["hosted_subnets"][0]["fast_path_ip"] == "100.80.0.1"

    alice = _user(db_session, "alice")
    _proxy(db_session, alice, location, subnet, "px", "alice1")
    body = _config(client, "r1")
    assert body["fast_path"]["tenants"] == []
    assert body["version"] != plain_version

    created = client.post(
        "/api/admin/private-networks",
        json={"owner_user_id": alice.id, "location_id": location.id, "kind": "proxy_access"},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    assert created.json()["kind"] == "proxy_access"
    assert [a["proxy_runner_name"] for a in created.json()["attachments"]] == ["r1"]
    tenant = _config(client, "r1")["fast_path"]["tenants"][0]
    assert tenant["addresses"] == ["100.80.0.1"]
    assert tenant["assignments"][0]["username"] == "alice1"

    unpinned = client.patch(f"/api/ipam/subnets/{subnet.id}", json={"clear_proxy_runner": True}, headers=admin)
    assert unpinned.json()["fast_path"] is None and unpinned.json()["fast_path_ip"] == "100.80.0.1"
    assert _config(client, "r1")["fast_path"]["tenants"][0]["addresses"] == []

    deleted = client.delete(f"{RUNNERS}/{runner.id}", headers=admin)
    assert deleted.status_code == 204
    assert PrivateNetworkDAO.list_attachments(db_session) == []


def test_client_turns_fast_path_on_and_off_and_never_sees_runners(client, db_session, location):
    runner = _runner(db_session, "r1")
    runner.location_id = location.id
    db_session.commit()
    subnet = IPAMDAO.create_subnet(db_session, name="a", cidr="203.0.113.0/29", location_id=location.id)
    subnet.proxy_runner_id, subnet.fast_path_ip = runner.id, "100.80.0.1"
    db_session.commit()
    alice = _user(db_session, "alice")
    _user(db_session, "bob")
    _proxy(db_session, alice, location, subnet, "px", "alice1")
    alice_h, bob_h = _login(client, "alice", "secret123"), _login(client, "bob", "secret123")

    view = client.get(f"{CLIENT}/fast-path", headers=alice_h).json()
    assert view["cidr"] == "100.80.0.0/16"
    assert view["locations"][0]["enabled"] is False
    assert view["locations"][0]["eligible"] is False
    assert view["locations"][0]["reason"] == "The proxy fast path needs an active server in this location"
    assert view["endpoints"][0]["fast_path_ip"] == "100.80.0.1" and view["endpoints"][0]["active"] is False
    refused = client.post(f"{CLIENT}/fast-path", json={"location_id": location.id}, headers=alice_h)
    assert refused.status_code == 409 and "active server" in refused.json()["detail"]

    _service(db_session, alice, location, "alice-bm", ServiceType.BARE_METAL)
    assert client.get(f"{CLIENT}/fast-path", headers=alice_h).json()["locations"][0]["eligible"] is True
    on = client.post(f"{CLIENT}/fast-path", json={"location_id": location.id}, headers=alice_h)
    assert on.status_code == 200, on.text
    assert on.json()["endpoints"][0]["active"] is True
    network_id = on.json()["locations"][0]["network_id"]

    detail = client.get(f"{CLIENT}/{network_id}", headers=alice_h).json()
    assert detail["kind"] == "proxy_access" and detail["attachments"] == []
    assert detail["storage_endpoint_ip"] == ""
    runner_attachment = PrivateNetworkDAO.list_attachments(db_session)[0]
    refused = client.delete(f"{CLIENT}/{network_id}/attachments/{runner_attachment.id}", headers=alice_h)
    assert refused.status_code == 404

    assert client.get(f"{CLIENT}/{network_id}", headers=bob_h).status_code == 404
    assert client.get(f"{CLIENT}/fast-path", headers=bob_h).json()["endpoints"] == []
    assert client.delete(f"{CLIENT}/fast-path/{location.id}", headers=bob_h).status_code == 404

    off = client.delete(f"{CLIENT}/fast-path/{location.id}", headers=alice_h)
    assert off.status_code == 200, off.text
    assert off.json()["locations"][0]["enabled"] is False


def test_proxy_assignment_payload_carries_fast_path(client, db_session, location):
    from app.services.proxy_provisioning import assignment_payload

    runner = _runner(db_session, "r1")
    runner.location_id = location.id
    db_session.commit()
    subnet = IPAMDAO.create_subnet(db_session, name="a", cidr="203.0.113.0/29", location_id=location.id)
    alice = _user(db_session, "alice")
    _, assignment = _proxy(db_session, alice, location, subnet, "px", "alice1")
    assert assignment_payload(assignment)["fast_path"] is None

    subnet.proxy_runner_id, subnet.fast_path_ip = runner.id, "100.80.0.7"
    db_session.commit()
    fast = assignment_payload(assignment)["fast_path"]
    assert fast["http_url"] == "http://alice1:secret-pw-1@100.80.0.7:11080"
    assert fast["endpoint"] == "100.80.0.7:11080:alice1:secret-pw-1"
    assert fast["active"] is False
