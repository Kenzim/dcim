"""
Tests for server boot API endpoints
"""
import pytest
from app.dao import BootTaskDAO, NetworkPortDAO, ServerDAO
from app.models.network_port import NetworkPort
from app.models.server import Server
from app.models.location import Location


@pytest.fixture
def test_location(db_session):
    """Create a test location"""
    location = Location(name="Test Location", description="Test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)
    return location


@pytest.fixture
def test_server(db_session, test_location):
    """Create a test server (uses plugin_name)"""
    server = Server(
        name="test-server",
        server_ip="192.168.1.100",
        location_id=test_location.id,
        plugin_name="ipmi",
        plugin_config={"hostname": "192.168.1.100"}
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


@pytest.fixture
def test_pxe_port(db_session, test_server):
    """Create a test network port with PXE boot enabled"""
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        mac_address="00:0e:1e:6f:16:b0",
        speed_mbps=1000,
        pxe_boot=True,
        pxe_ip="192.168.1.100"
    )
    return port


def test_get_pxe_boot_file_success(client, db_session, test_pxe_port):
    """Test getting PXE boot file for a valid MAC address"""
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00:0e:1e:6f:16:b0"}
    )
    
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert "#!ipxe" in response.text
    # When no boot task exists, should return exit script
    assert "exit" in response.text.lower() or "local disk" in response.text.lower()


def test_get_pxe_boot_file_mac_normalization(client, db_session, test_pxe_port):
    """Test that MAC address normalization works"""
    # Test with dashes
    response1 = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00-0e-1e-6f-16-b0"}
    )
    assert response1.status_code == 200
    
    # Test without separators
    response2 = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "000e1e6f16b0"}
    )
    assert response2.status_code == 200


def test_get_pxe_boot_file_missing_mac(client):
    """Test that missing MAC address returns 400"""
    response = client.get("/api/servers/interaction/pxe")
    assert response.status_code == 400
    assert "required" in response.json()["detail"].lower() or "mac" in response.json()["detail"].lower()


def test_get_pxe_boot_file_unknown_mac(client):
    """Test that unknown MAC address returns 404"""
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "aa:bb:cc:dd:ee:ff"}
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower() or "no server found" in response.json()["detail"].lower()


def test_get_pxe_boot_file_port_not_pxe_enabled(client, db_session, test_server):
    """Test that port without PXE boot enabled returns 400"""
    # Create a port without PXE boot
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth1",
        mac_address="aa:bb:cc:dd:ee:ff",
        speed_mbps=1000,
        pxe_boot=False
    )
    
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "aa:bb:cc:dd:ee:ff"}
    )
    assert response.status_code == 400
    assert "not configured for PXE boot" in response.json()["detail"]


@pytest.fixture
def admin_client(client, test_admin_user):
    response = client.post("/api/users/login", json={
        "username": test_admin_user.username,
        "password": "adminpassword123"
    })
    assert response.status_code == 200
    return client


def test_get_pxe_info_requires_admin(client, test_pxe_port):
    response = client.get(
        "/api/servers/interaction/pxe/info",
        params={"mac": "00:0e:1e:6f:16:b0"}
    )
    assert response.status_code in (401, 403)


def test_get_pxe_info_success(admin_client, db_session, test_pxe_port, test_server):
    """Test getting PXE info for a valid MAC address"""
    client = admin_client
    response = client.get(
        "/api/servers/interaction/pxe/info",
        params={"mac": "00:0e:1e:6f:16:b0"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["server_id"] == test_server.id
    assert data["server_name"] == test_server.name
    # MAC address is normalized to uppercase
    assert data["mac_address"].upper() == "00:0E:1E:6F:16:B0"
    assert data["pxe_boot"] is True
    assert data["pxe_ip"] == "192.168.1.100"
    assert "/api/servers/interaction/pxe" in data["boot_file_url"]


def test_get_pxe_info_missing_mac(admin_client):
    """Test that missing MAC address returns 400"""
    response = admin_client.get("/api/servers/interaction/pxe/info")
    assert response.status_code == 422  # FastAPI returns 422 for missing required query parameters
    detail = response.json()["detail"]
    # Check if it's a validation error about missing field
    assert any("mac" in str(err).lower() for err in detail)


def test_get_pxe_info_unknown_mac(admin_client):
    """Test that unknown MAC address returns 404"""
    response = admin_client.get(
        "/api/servers/interaction/pxe/info",
        params={"mac": "aa:bb:cc:dd:ee:ff"}
    )
    assert response.status_code == 404


def _temp_os_task(db_session, server_id):
    return BootTaskDAO.create(
        db=db_session, server_id=server_id, boot_type="linux_script",
        script_content="#!/bin/sh\necho hi\n",
    )


def test_pxe_boot_task_gets_nonce(db_session, test_server):
    task = _temp_os_task(db_session, test_server.id)
    assert task.pxe_nonce and len(task.pxe_nonce) >= 24
    assert task.pxe_nonce_used_at is None


def test_pxe_with_valid_nonce_and_ip_serves_once(pxe_get, db_session, test_pxe_port, test_server):
    task = _temp_os_task(db_session, test_server.id)
    first = pxe_get("00:0e:1e:6f:16:b0", task)
    assert first.status_code == 200
    assert "#!ipxe" in first.text and "kernel" in first.text
    db_session.refresh(task)
    assert task.pxe_nonce_used_at is not None
    replay = pxe_get("00:0e:1e:6f:16:b0", task)
    assert replay.status_code == 403


def test_pxe_without_nonce_is_forbidden(pxe_get, db_session, test_pxe_port, test_server):
    _temp_os_task(db_session, test_server.id)
    response = pxe_get("00:0e:1e:6f:16:b0")
    assert response.status_code == 403
    assert "kernel" not in response.text


def test_pxe_wrong_nonce_is_forbidden(pxe_get, db_session, test_pxe_port, test_server):
    task = _temp_os_task(db_session, test_server.id)
    response = pxe_get("00:0e:1e:6f:16:b0", task, nonce="not-the-nonce")
    assert response.status_code == 403
    db_session.refresh(task)
    assert task.pxe_nonce_used_at is None  # a bad attempt must not burn the nonce


def test_pxe_wrong_source_ip_is_forbidden(pxe_get, db_session, test_pxe_port, test_server):
    """A spoofed MAC with the right nonce from another address is refused."""
    task = _temp_os_task(db_session, test_server.id)
    response = pxe_get("00:0e:1e:6f:16:b0", task, source_ip="192.168.1.101")
    assert response.status_code == 403
    db_session.refresh(task)
    assert task.pxe_nonce_used_at is None


def test_pxe_ignores_forwarded_for_unless_trusted(client, db_session, test_pxe_port, test_server):
    task = _temp_os_task(db_session, test_server.id)
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00:0e:1e:6f:16:b0", "t": task.pxe_nonce},
        headers={"X-Forwarded-For": "192.168.1.100"},
    )
    assert response.status_code == 403


def test_pxe_schedules_dhcp_sync_after_serve(pxe_get, db_session, test_pxe_port, test_server, _no_background_dhcp_sync):
    task = _temp_os_task(db_session, test_server.id)
    _no_background_dhcp_sync.clear()
    assert pxe_get("00:0e:1e:6f:16:b0", task).status_code == 200
    assert _no_background_dhcp_sync == [test_server.id]


def test_boot_task_create_schedules_dhcp_sync(db_session, test_server, _no_background_dhcp_sync):
    _temp_os_task(db_session, test_server.id)
    assert _no_background_dhcp_sync == [test_server.id]
