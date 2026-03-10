"""
Tests for boot script serving in server interaction API
"""
import pytest
import os
import tempfile
from pathlib import Path
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


def test_boot_script_file_not_found(client, db_session, test_pxe_port):
    """Test that when no boot task exists, returns exit script (not error)"""
    # The current implementation doesn't read from boot.ipxe file anymore
    # It returns an exit script when no boot task exists
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00:0e:1e:6f:16:b0"}
    )
    
    assert response.status_code == 200
    assert "#!ipxe" in response.text
    assert "exit" in response.text.lower()


def test_boot_script_content_served(client, db_session, test_pxe_port, test_server):
    """Test that boot script is correctly served when boot task exists"""
    from app.dao import BootTaskDAO
    
    # Create a boot task
    boot_task = BootTaskDAO.create(
        db=db_session,
        server_id=test_server.id,
        boot_type="linux_script",
        script_content="#!/bin/sh\necho 'Test script'\n",
        description="Test boot task"
    )
    
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00:0e:1e:6f:16:b0"}
    )
    
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert "#!ipxe" in response.text
    # Should contain boot script for Linux script boot task
    assert "Booting Linux" in response.text or "kernel" in response.text.lower()


def test_boot_script_response_headers(client, db_session, test_pxe_port):
    """Test that boot script response has correct headers"""
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00:0e:1e:6f:16:b0"}
    )
    
    assert response.status_code == 200
    assert "content-type" in response.headers
    assert "text/plain" in response.headers["content-type"]
    # Check that Content-Disposition header includes server name
    assert "Content-Disposition" in response.headers or "content-disposition" in response.headers
