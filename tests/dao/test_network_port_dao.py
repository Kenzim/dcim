"""
Tests for NetworkPortDAO
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


def test_create_network_port(db_session, test_server):
    """Test creating a network port via DAO"""
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000,
        mac_address="00:0e:1e:6f:16:b0"
    )
    
    assert port.id is not None
    assert port.server_id == test_server.id
    assert port.name == "eth0"
    assert port.speed_mbps == 1000
    assert port.mac_address == "00:0e:1e:6f:16:b0"


def test_create_network_port_with_all_fields(db_session, test_server):
    """Test creating a network port with all fields"""
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        speed_mbps=10000,
        mac_address="00:0e:1e:6f:16:b0",
        lag_group="bond0",
        monitor_bandwidth=True,
        pxe_boot=True,
        pxe_ip="192.168.1.100",
        description="Primary network interface"
    )
    
    assert port.name == "eth0"
    assert port.speed_mbps == 10000
    assert port.mac_address == "00:0e:1e:6f:16:b0"
    assert port.lag_group == "bond0"
    assert port.monitor_bandwidth is True
    assert port.pxe_boot is True
    assert port.pxe_ip == "192.168.1.100"
    assert port.description == "Primary network interface"


def test_get_network_port_by_id(db_session, test_server):
    """Test getting a network port by ID"""
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000
    )
    
    retrieved = NetworkPortDAO.get_by_id(db_session, port.id)
    assert retrieved is not None
    assert retrieved.id == port.id
    assert retrieved.name == "eth0"


def test_get_network_ports_by_server(db_session, test_server):
    """Test getting all network ports for a server"""
    port1 = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000
    )
    port2 = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth1",
        speed_mbps=10000
    )
    
    ports = NetworkPortDAO.get_by_server(db_session, test_server.id)
    assert len(ports) == 2
    port_names = [p.name for p in ports]
    assert "eth0" in port_names
    assert "eth1" in port_names


def test_update_network_port(db_session, test_server):
    """Test updating a network port"""
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000
    )
    
    port.name = "eth1"
    port.speed_mbps = 10000
    updated = NetworkPortDAO.update(db_session, port)
    
    assert updated.name == "eth1"
    assert updated.speed_mbps == 10000


def test_delete_network_port(db_session, test_server):
    """Test deleting a network port"""
    port = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000
    )
    port_id = port.id
    
    result = NetworkPortDAO.delete(db_session, port_id)
    assert result is True
    
    deleted = NetworkPortDAO.get_by_id(db_session, port_id)
    assert deleted is None


def test_delete_network_ports_by_server(db_session, test_server):
    """Test deleting all network ports for a server"""
    NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000
    )
    NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth1",
        speed_mbps=1000
    )
    
    deleted_count = NetworkPortDAO.delete_by_server(db_session, test_server.id)
    assert deleted_count == 2
    
    ports = NetworkPortDAO.get_by_server(db_session, test_server.id)
    assert len(ports) == 0


def test_get_network_ports_ordered_by_lag_group(db_session, test_server):
    """Test that network ports are ordered by lag_group and name"""
    port1 = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth1",
        speed_mbps=1000,
        lag_group="bond0"
    )
    port2 = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000,
        lag_group="bond0"
    )
    port3 = NetworkPortDAO.create(
        db_session,
        server_id=test_server.id,
        name="eth2",
        speed_mbps=1000
    )
    
    ports = NetworkPortDAO.get_by_server(db_session, test_server.id)
    assert len(ports) == 3
    
    # Ports should be ordered by lag_group (NULLs last) and then by name
    # Ports with lag_group="bond0" should come first, ordered by name
    port_names = [p.name for p in ports]
    assert "eth0" in port_names
    assert "eth1" in port_names
    assert "eth2" in port_names
    
    # Ports with lag_group should be grouped together
    lag_ports = [p for p in ports if p.lag_group == "bond0"]
    assert len(lag_ports) == 2
    # Within lag_group, should be ordered by name
    lag_names = [p.name for p in lag_ports]
    assert lag_names == sorted(lag_names)
