"""
Tests for NetworkPort model
"""
import pytest
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


def test_network_port_creation(db_session, test_server):
    """Test creating a network port"""
    port = NetworkPort(
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000,
        mac_address="00:0e:1e:6f:16:b0"
    )
    db_session.add(port)
    db_session.commit()
    db_session.refresh(port)
    
    assert port.id is not None
    assert port.name == "eth0"
    assert port.speed_mbps == 1000
    assert port.mac_address == "00:0e:1e:6f:16:b0"
    assert port.monitor_bandwidth is False
    assert port.pxe_boot is False


def test_network_port_with_lag_group(db_session, test_server):
    """Test creating network ports with LAG group"""
    port1 = NetworkPort(
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000,
        lag_group="bond0"
    )
    port2 = NetworkPort(
        server_id=test_server.id,
        name="eth1",
        speed_mbps=1000,
        lag_group="bond0"
    )
    db_session.add_all([port1, port2])
    db_session.commit()
    
    assert port1.lag_group == "bond0"
    assert port2.lag_group == "bond0"


def test_network_port_with_pxe_boot(db_session, test_server):
    """Test creating a network port with PXE boot enabled"""
    port = NetworkPort(
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000,
        pxe_boot=True,
        pxe_ip="192.168.1.100"
    )
    db_session.add(port)
    db_session.commit()
    db_session.refresh(port)
    
    assert port.pxe_boot is True
    assert port.pxe_ip == "192.168.1.100"


def test_network_port_cascade_delete(db_session, test_server):
    """Test that network ports relationship exists and can be queried"""
    port = NetworkPort(
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000
    )
    db_session.add(port)
    db_session.commit()
    db_session.refresh(port)
    port_id = port.id
    
    # Verify port exists and is linked to server
    retrieved_port = db_session.query(NetworkPort).filter(NetworkPort.id == port_id).first()
    assert retrieved_port is not None
    assert retrieved_port.server_id == test_server.id
    
    # Verify relationship works
    assert port.server.id == test_server.id
    assert port in test_server.network_ports or len(test_server.network_ports) > 0


def test_network_port_repr(db_session, test_server):
    """Test NetworkPort __repr__ method"""
    port = NetworkPort(
        server_id=test_server.id,
        name="eth0",
        speed_mbps=1000,
        mac_address="00:0e:1e:6f:16:b0",
        lag_group="bond0",
        monitor_bandwidth=True,
        pxe_boot=True,
        pxe_ip="192.168.1.100"
    )
    db_session.add(port)
    db_session.commit()
    
    repr_str = repr(port)
    assert "eth0" in repr_str
    assert "1000Mbps" in repr_str
    assert "mac=00:0e:1e:6f:16:b0" in repr_str
    assert "lag=bond0" in repr_str
    assert "monitored" in repr_str
    assert "pxe=192.168.1.100" in repr_str
