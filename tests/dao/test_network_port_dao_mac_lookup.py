"""
Tests for MAC address lookup in NetworkPortDAO
"""
import pytest
from app.dao import NetworkPortDAO
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


def test_get_by_mac_address_colon_format(db_session, test_server):
    """Test getting network port by MAC address with colon format"""
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        mac_address="00:0e:1e:6f:16:b0",
        speed_mbps=1000
    )
    
    found = NetworkPortDAO.get_by_mac_address(db_session, "00:0e:1e:6f:16:b0")
    assert found is not None
    assert found.id == port.id
    assert found.mac_address == "00:0e:1e:6f:16:b0"


def test_get_by_mac_address_dash_format(db_session, test_server):
    """Test getting network port by MAC address with dash format"""
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        mac_address="00:0e:1e:6f:16:b0",
        speed_mbps=1000
    )
    
    # Should find port even with dash format (normalization happens internally)
    found = NetworkPortDAO.get_by_mac_address(db_session, "00-0e-1e-6f-16-b0")
    assert found is not None
    assert found.id == port.id


def test_get_by_mac_address_no_separators(db_session, test_server):
    """Test getting network port by MAC address without separators"""
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        mac_address="00:0e:1e:6f:16:b0",
        speed_mbps=1000
    )
    
    # Should find port even without separators (normalization happens internally)
    found = NetworkPortDAO.get_by_mac_address(db_session, "000e1e6f16b0")
    assert found is not None
    assert found.id == port.id


def test_get_by_mac_address_case_insensitive(db_session, test_server):
    """Test that MAC address lookup is case-insensitive"""
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        mac_address="00:0e:1e:6f:16:b0",
        speed_mbps=1000
    )
    
    # Test uppercase
    found = NetworkPortDAO.get_by_mac_address(db_session, "00:0E:1E:6F:16:B0")
    assert found is not None
    assert found.id == port.id
    
    # Test mixed case
    found = NetworkPortDAO.get_by_mac_address(db_session, "00:0E:1e:6F:16:b0")
    assert found is not None
    assert found.id == port.id


def test_get_by_mac_address_not_found(db_session, test_server):
    """Test that get_by_mac_address returns None for non-existent MAC"""
    found = NetworkPortDAO.get_by_mac_address(db_session, "aa:bb:cc:dd:ee:ff")
    assert found is None


def test_get_by_mac_address_multiple_ports(db_session, test_server):
    """Test that get_by_mac_address finds the correct port when multiple exist"""
    port1 = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        mac_address="00:0e:1e:6f:16:b0",
        speed_mbps=1000
    )
    port2 = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth1",
        mac_address="aa:bb:cc:dd:ee:ff",
        speed_mbps=10000
    )
    
    found = NetworkPortDAO.get_by_mac_address(db_session, "00:0e:1e:6f:16:b0")
    assert found is not None
    assert found.id == port1.id
    assert found.name == "eth0"
    
    found = NetworkPortDAO.get_by_mac_address(db_session, "aa:bb:cc:dd:ee:ff")
    assert found is not None
    assert found.id == port2.id
    assert found.name == "eth1"


def test_get_by_mac_address_with_null_mac(db_session, test_server):
    """Test that ports without MAC address are not found"""
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000,
        mac_address=None
    )
    
    found = NetworkPortDAO.get_by_mac_address(db_session, "00:0e:1e:6f:16:b0")
    assert found is None
