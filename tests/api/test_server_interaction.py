"""
Tests for server boot API endpoints
"""
import pytest
from app.dao import NetworkPortDAO, ServerDAO
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


def test_get_pxe_info_success(client, db_session, test_pxe_port, test_server):
    """Test getting PXE info for a valid MAC address"""
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


def test_get_pxe_info_missing_mac(client):
    """Test that missing MAC address returns 400"""
    response = client.get("/api/servers/interaction/pxe/info")
    assert response.status_code == 422  # FastAPI returns 422 for missing required query parameters
    detail = response.json()["detail"]
    # Check if it's a validation error about missing field
    assert any("mac" in str(err).lower() for err in detail)


def test_get_pxe_info_unknown_mac(client):
    """Test that unknown MAC address returns 404"""
    response = client.get(
        "/api/servers/interaction/pxe/info",
        params={"mac": "aa:bb:cc:dd:ee:ff"}
    )
    assert response.status_code == 404
