"""
Tests for PXE script endpoint (script=true parameter for custom initramfs)
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.dao import BootTaskDAO, NetworkPortDAO, ServerDAO
from app.models.boot_task import BootType, BootTaskStatus
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


def test_get_pxe_script_content(client, db_session, test_pxe_port, test_server):
    """Test getting script content directly (for custom initramfs)"""
    # Create boot task with script content
    boot_task = BootTaskDAO.create(
        db=db_session,
        server_id=test_server.id,
        boot_type="linux_script",
        script_content="#!/bin/sh\necho 'Hello from script'\n",
        description="Test script"
    )
    
    # Get script content with script=true parameter
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00:0e:1e:6f:16:b0", "script": "true"}
    )
    
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert "#!/bin/sh" in response.text
    assert "Hello from script" in response.text
    
    # Check that boot task was marked as in_progress
    db_session.refresh(boot_task)
    assert boot_task.status == BootTaskStatus.IN_PROGRESS


def test_get_pxe_script_content_no_boot_task(client, db_session, test_pxe_port):
    """Test getting script content when no boot task exists"""
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00:0e:1e:6f:16:b0", "script": "true"}
    )
    
    # When script=true but no boot task exists, endpoint returns 404
    # (The endpoint checks for boot_task before handling script parameter)
    assert response.status_code == 404


def test_get_pxe_script_content_wrong_boot_type(client, db_session, test_pxe_port, test_server):
    """Test getting script content for non-linux_script boot type"""
    # Create ISO boot task
    boot_task = BootTaskDAO.create(
        db=db_session,
        server_id=test_server.id,
        boot_type="iso",
        iso_url="http://example.com/test.iso"
    )
    
    # Try to get script content
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00:0e:1e:6f:16:b0", "script": "true"}
    )
    
    # Should return 404 for non-linux_script boot types
    assert response.status_code == 404


def test_get_pxe_script_content_no_script_content(client, db_session, test_pxe_port, test_server):
    """Test getting script content when boot task has no script_content"""
    # Create boot task without script content
    boot_task = BootTaskDAO.create(
        db=db_session,
        server_id=test_server.id,
        boot_type="linux_script",
        script_url="http://example.com/script.sh",
        description="Test script"
    )
    
    # Try to get script content
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00:0e:1e:6f:16:b0", "script": "true"}
    )
    
    # Should return 404 when no script_content
    assert response.status_code == 404


def test_get_pxe_script_content_normal_pxe_request(client, db_session, test_pxe_port, test_server):
    """Test that normal PXE request (without script=true) returns iPXE script"""
    # Create boot task
    boot_task = BootTaskDAO.create(
        db=db_session,
        server_id=test_server.id,
        boot_type="linux_script",
        script_content="#!/bin/sh\necho 'Hello'\n"
    )
    
    # Get normal PXE boot script (without script=true)
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00:0e:1e:6f:16:b0"}
    )
    
    assert response.status_code == 200
    # Should return iPXE script, not the script content
    assert "#!ipxe" in response.text
    assert "#!/bin/sh" not in response.text
