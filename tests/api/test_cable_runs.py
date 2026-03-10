"""
Tests for cable run API (device-agnostic: two ports, each switch or server).
"""
import pytest
from app.dao import (
    CableRunDAO,
    SwitchPortDAO,
    NetworkPortDAO,
    NetworkSwitchDAO,
    LocationDAO,
)
from app.models.server import Server
from app.models.location import Location
from app.models.network_switch import NetworkSwitch
from app.models.switch_port import SwitchPort
from app.models.network_port import NetworkPort


@pytest.fixture
def test_location(db_session):
    loc = Location(name="Test DC", description="Test")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)
    return loc


@pytest.fixture
def test_server(db_session, test_location):
    server = Server(
        name="test-server",
        server_ip="192.168.1.10",
        location_id=test_location.id,
        plugin_name="ipmi",
        plugin_config={"hostname": "192.168.1.10"},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


@pytest.fixture
def test_server_port(db_session, test_server):
    port = NetworkPort(
        server_id=test_server.id,
        name="Port 1",
        mac_address="AC:1F:6B:58:87:20",
        speed_mbps=10000,
    )
    db_session.add(port)
    db_session.commit()
    db_session.refresh(port)
    return port


@pytest.fixture
def test_switch(db_session, test_location):
    sw = NetworkSwitch(
        name="test-switch",
        location_id=test_location.id,
        plugin_name="snmpv3",
        plugin_config={"hostname": "192.168.1.1", "username": "u", "auth_password": "p", "privacy_password": "p"},
        enabled=True,
    )
    db_session.add(sw)
    db_session.commit()
    db_session.refresh(sw)
    return sw


@pytest.fixture
def test_switch_port(db_session, test_switch):
    port = SwitchPort(
        switch_id=test_switch.id,
        name="TenGigabitEthernet1",
        speed_mbps=10000,
    )
    db_session.add(port)
    db_session.commit()
    db_session.refresh(port)
    return port


def _login_admin(client, test_admin_user):
    r = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert r.status_code == 200
    return r.json()["token"]


def test_list_cable_runs_by_server_empty(
    client, test_admin_user, test_server, test_server_port, mock_redis
):
    """List cable runs for a server with no connections returns empty list."""
    token = _login_admin(client, test_admin_user)
    r = client.get(
        "/api/cable-runs/",
        params={"server_id": test_server.id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json() == []


def test_create_cable_run_and_list_by_server(
    client, db_session, test_admin_user,
    test_server, test_server_port,
    test_switch, test_switch_port,
    mock_redis,
):
    """Create a cable run (port_a/port_b) and list by server_id; response has end_a/end_b with device names."""
    token = _login_admin(client, test_admin_user)
    r = client.post(
        "/api/cable-runs/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "port_a": {"type": "switch", "id": test_switch_port.id},
            "port_b": {"type": "server", "id": test_server_port.id},
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert "end_a" in data and "end_b" in data
    ends = {data["end_a"]["type"]: data["end_a"], data["end_b"]["type"]: data["end_b"]}
    assert ends["switch"]["id"] == test_switch_port.id
    assert ends["switch"]["port_name"] == test_switch_port.name
    assert ends["switch"]["device_id"] == test_switch.id
    assert ends["switch"]["device_name"] == test_switch.name
    assert ends["server"]["id"] == test_server_port.id
    assert ends["server"]["port_name"] == test_server_port.name
    assert ends["server"]["device_id"] == test_server.id
    assert ends["server"]["device_name"] == test_server.name

    r2 = client.get(
        "/api/cable-runs/",
        params={"server_id": test_server.id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    runs = r2.json()
    assert len(runs) == 1
    run = runs[0]
    server_end = run["end_a"] if run["end_a"]["type"] == "server" else run["end_b"]
    assert server_end["id"] == test_server_port.id
    assert server_end["device_name"] == test_server.name


def test_create_cable_run_rejects_duplicate_switch_port(
    client, db_session, test_admin_user,
    test_server, test_server_port,
    test_switch, test_switch_port,
    mock_redis,
):
    """Creating a second cable run for the same switch port returns 400."""
    token = _login_admin(client, test_admin_user)
    CableRunDAO.create(
        db_session,
        port_a_type="switch",
        port_a_id=test_switch_port.id,
        port_b_type="server",
        port_b_id=test_server_port.id,
    )
    db_session.commit()

    port2 = NetworkPort(
        server_id=test_server.id,
        name="Port 2",
        speed_mbps=1000,
    )
    db_session.add(port2)
    db_session.commit()
    db_session.refresh(port2)

    r = client.post(
        "/api/cable-runs/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "port_a": {"type": "switch", "id": test_switch_port.id},
            "port_b": {"type": "server", "id": port2.id},
        },
    )
    assert r.status_code == 400
    assert "already connected" in r.json().get("detail", "").lower()


def test_create_cable_run_rejects_duplicate_server_port(
    client, db_session, test_admin_user,
    test_server, test_server_port,
    test_switch, test_switch_port,
    mock_redis,
):
    """Creating a second cable run for the same server port returns 400."""
    token = _login_admin(client, test_admin_user)
    CableRunDAO.create(
        db_session,
        port_a_type="switch",
        port_a_id=test_switch_port.id,
        port_b_type="server",
        port_b_id=test_server_port.id,
    )
    db_session.commit()

    port2 = SwitchPort(
        switch_id=test_switch.id,
        name="TenGigabitEthernet2",
    )
    db_session.add(port2)
    db_session.commit()
    db_session.refresh(port2)

    r = client.post(
        "/api/cable-runs/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "port_a": {"type": "switch", "id": port2.id},
            "port_b": {"type": "server", "id": test_server_port.id},
        },
    )
    assert r.status_code == 400
    assert "already connected" in r.json().get("detail", "").lower()


def test_delete_cable_run(
    client, db_session, test_admin_user,
    test_server, test_server_port,
    test_switch, test_switch_port,
    mock_redis,
):
    """Delete cable run and list by server returns empty."""
    token = _login_admin(client, test_admin_user)
    cr = CableRunDAO.create(
        db_session,
        port_a_type="switch",
        port_a_id=test_switch_port.id,
        port_b_type="server",
        port_b_id=test_server_port.id,
    )
    db_session.commit()

    r = client.delete(
        f"/api/cable-runs/{cr.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204

    r2 = client.get(
        "/api/cable-runs/",
        params={"server_id": test_server.id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    assert r2.json() == []


def test_switch_ports_response_includes_cable_run_with_server_name(
    client, db_session, test_admin_user,
    test_server, test_server_port,
    test_switch, test_switch_port,
    mock_redis,
):
    """GET switch ports returns cable_run with other_end_* when connected to server. """
    token = _login_admin(client, test_admin_user)
    CableRunDAO.create(
        db_session,
        port_a_type="switch",
        port_a_id=test_switch_port.id,
        port_b_type="server",
        port_b_id=test_server_port.id,
    )
    db_session.commit()

    r = client.get(
        f"/api/network-switches/{test_switch.id}/switch-ports",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    ports = data.get("ports", [])
    assert len(ports) >= 1
    port1 = next((p for p in ports if p["id"] == test_switch_port.id), None)
    assert port1 is not None
    assert port1.get("cable_run") is not None
    cr = port1["cable_run"]
    assert cr["other_end_type"] == "server"
    assert cr["other_end_port_id"] == test_server_port.id
    assert cr["other_end_device_id"] == test_server.id
    assert cr["other_end_device_name"] == test_server.name
    assert cr["other_end_port_name"] == test_server_port.name


def test_list_cable_runs_by_server_id_query_param_as_string(
    client, test_admin_user,
    test_server, test_server_port,
    test_switch, test_switch_port,
    mock_redis,
):
    """List cable runs with server_id passed as string (e.g. from URL) still returns runs."""
    token = _login_admin(client, test_admin_user)
    r_create = client.post(
        "/api/cable-runs/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "port_a": {"type": "switch", "id": test_switch_port.id},
            "port_b": {"type": "server", "id": test_server_port.id},
        },
    )
    assert r_create.status_code == 201

    # Request with server_id as string (as URL query params often are)
    r = client.get(
        "/api/cable-runs/",
        params={"server_id": str(test_server.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["end_a"]["type"] != runs[0]["end_b"]["type"]
    server_end = runs[0]["end_a"] if runs[0]["end_a"]["type"] == "server" else runs[0]["end_b"]
    assert server_end["id"] == test_server_port.id
    assert server_end["device_id"] == test_server.id


def test_list_cable_runs_by_switch_id(
    client, test_admin_user,
    test_server, test_server_port,
    test_switch, test_switch_port,
    mock_redis,
):
    """List cable runs filtered by switch_id returns runs for that switch."""
    token = _login_admin(client, test_admin_user)
    client.post(
        "/api/cable-runs/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "port_a": {"type": "switch", "id": test_switch_port.id},
            "port_b": {"type": "server", "id": test_server_port.id},
        },
    )

    r = client.get(
        "/api/cable-runs/",
        params={"switch_id": test_switch.id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    switch_end = runs[0]["end_a"] if runs[0]["end_a"]["type"] == "switch" else runs[0]["end_b"]
    assert switch_end["id"] == test_switch_port.id
    assert switch_end["device_id"] == test_switch.id


def test_list_cable_runs_unfiltered(
    client, test_admin_user,
    test_server, test_server_port,
    test_switch, test_switch_port,
    mock_redis,
):
    """List cable runs with no filter returns all runs (at least the one we create)."""
    token = _login_admin(client, test_admin_user)
    client.post(
        "/api/cable-runs/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "port_a": {"type": "switch", "id": test_switch_port.id},
            "port_b": {"type": "server", "id": test_server_port.id},
        },
    )
    r = client.get(
        "/api/cable-runs/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) >= 1


def test_get_cable_run_by_id(
    client, test_admin_user,
    test_server, test_server_port,
    test_switch, test_switch_port,
    mock_redis,
):
    """GET /cable-runs/{id} returns the cable run."""
    token = _login_admin(client, test_admin_user)
    r_create = client.post(
        "/api/cable-runs/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "port_a": {"type": "switch", "id": test_switch_port.id},
            "port_b": {"type": "server", "id": test_server_port.id},
        },
    )
    assert r_create.status_code == 201
    cr_id = r_create.json()["id"]

    r = client.get(
        f"/api/cable-runs/{cr_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == cr_id
    assert data["end_a"]["type"] != data["end_b"]["type"]


def test_update_cable_run_metadata(
    client, test_admin_user,
    test_server, test_server_port,
    test_switch, test_switch_port,
    mock_redis,
):
    """PUT /cable-runs/{id} updates cable_type, speed_mbps, description."""
    token = _login_admin(client, test_admin_user)
    r_create = client.post(
        "/api/cable-runs/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "port_a": {"type": "switch", "id": test_switch_port.id},
            "port_b": {"type": "server", "id": test_server_port.id},
            "cable_type": "DAC",
            "speed_mbps": 10000,
        },
    )
    assert r_create.status_code == 201
    cr_id = r_create.json()["id"]

    r = client.put(
        f"/api/cable-runs/{cr_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"cable_type": "AOC", "description": "Updated desc"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["cable_type"] == "AOC"
    assert data["description"] == "Updated desc"
    assert data["speed_mbps"] == 10000


def test_list_cable_runs_requires_auth(client, test_server):
    """GET /cable-runs/?server_id= without auth returns 403."""
    r = client.get("/api/cable-runs/", params={"server_id": test_server.id})
    assert r.status_code in (401, 403)
