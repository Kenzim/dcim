"""
Tests for boot task creation with temporary OS
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.dao import BootTaskDAO, ServerDAO
from app.models.boot_task import BootType, BootTaskStatus
from app.models.server import Server
from app.models.plugin import Plugin
from app.models.location import Location
from app.services.temp_os_service import TempOSService
import json
import tempfile
from pathlib import Path


@pytest.fixture
def temp_os_dir(tmp_path, monkeypatch):
    """Create a temporary directory structure for temp OSes and patch the service"""
    base_dir = tmp_path / "temp_os"
    base_dir.mkdir(parents=True)
    
    # Create Alpine config
    alpine_dir = base_dir / "alpine"
    alpine_dir.mkdir()
    (alpine_dir / "kernel").mkdir()
    (alpine_dir / "initrd").mkdir()
    (alpine_dir / "modloop").mkdir()
    
    alpine_config = {
        "id": "alpine",
        "name": "Alpine Linux",
        "description": "Lightweight Linux distribution",
        "kernel_file": "vmlinuz-virt",
        "initrd_file": "initramfs-virt",
        "modloop_file": "modloop-virt",
        "kernel_params": "ip=dhcp alpine_repo=http://dl-cdn.alpinelinux.org/alpine/latest-stable/main",
        "requires_modloop": True,
        "version": "3.23.0",
        "flavor": "virt"
    }
    
    with open(alpine_dir / "config.json", "w") as f:
        json.dump(alpine_config, f)
    
    (alpine_dir / "kernel" / "vmlinuz-virt").write_bytes(b"fake kernel")
    (alpine_dir / "initrd" / "initramfs-virt").write_bytes(b"fake initrd")
    (alpine_dir / "modloop" / "modloop-virt").write_bytes(b"fake modloop")
    
    # Patch the service to use our temp directory
    def get_temp_os_service():
        return TempOSService(base_dir=base_dir)
    
    monkeypatch.setattr("app.api.server_interaction.get_temp_os_service", get_temp_os_service)
    
    return base_dir


@pytest.fixture
def test_location(db_session):
    """Create a test location"""
    location = Location(name="Test Location", description="Test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)
    return location


@pytest.fixture
def test_plugin(db_session):
    """Create a test plugin"""
    plugin = Plugin(
        name="test_plugin",
        version="1.0.0",
        config_template={"type": "object", "properties": {}}
    )
    db_session.add(plugin)
    db_session.commit()
    db_session.refresh(plugin)
    return plugin


@pytest.fixture
def test_server(db_session, test_location, test_plugin):
    """Create a test server"""
    server = Server(
        name="test-server",
        server_ip="192.168.1.100",
        location_id=test_location.id,
        plugin_id=test_plugin.id,
        plugin_config={"hostname": "192.168.1.100"}
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


@pytest.fixture
def admin_client(client, test_admin_user):
    """Create an authenticated admin client"""
    response = client.post("/api/users/login", json={
        "username": test_admin_user.username,
        "password": "adminpassword123"
    })
    assert response.status_code == 200
    return client


def test_create_boot_task_temp_os(admin_client, test_server, temp_os_dir):
    """Test creating a boot task with temporary OS"""
    response = admin_client.post(
        f"/api/servers/interaction/servers/{test_server.id}/boot-task",
        json={
            "boot_type": "temp_os",
            "temp_os_id": "alpine",
            "description": "Boot into Alpine Linux"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["boot_type"] == "temp_os"
    assert data["temp_os_id"] == "alpine"
    assert data["description"] == "Boot into Alpine Linux"
    assert data["status"] == "pending"
    
    # Check that kernel and initrd URLs are set
    assert data["kernel_url"] is not None
    assert "temp-os/alpine/kernel" in data["kernel_url"]
    assert data["initrd_url"] is not None
    assert "temp-os/alpine/initrd" in data["initrd_url"]
    
    # Check that kernel params include modloop
    assert data["kernel_params"] is not None
    assert "modloop=" in data["kernel_params"]


def test_create_boot_task_temp_os_missing_id(admin_client, test_server, temp_os_dir):
    """Test creating boot task with temp_os type but missing temp_os_id"""
    response = admin_client.post(
        f"/api/servers/interaction/servers/{test_server.id}/boot-task",
        json={
            "boot_type": "temp_os",
            "description": "Boot into temporary OS"
        }
    )
    
    assert response.status_code == 400
    assert "temp_os_id" in response.json()["detail"].lower()


def test_create_boot_task_temp_os_invalid_id(admin_client, test_server, temp_os_dir):
    """Test creating boot task with non-existent temp_os_id"""
    response = admin_client.post(
        f"/api/servers/interaction/servers/{test_server.id}/boot-task",
        json={
            "boot_type": "temp_os",
            "temp_os_id": "nonexistent",
            "description": "Boot into temporary OS"
        }
    )
    
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_create_boot_task_temp_os_custom_initramfs(admin_client, test_server, temp_os_dir):
    """Test creating boot task with custom-initramfs"""
    # Create custom-initramfs config
    custom_dir = temp_os_dir / "custom-initramfs"
    custom_dir.mkdir()
    (custom_dir / "kernel").mkdir()
    (custom_dir / "initrd").mkdir()
    
    custom_config = {
        "id": "custom-initramfs",
        "name": "Custom Initramfs",
        "description": "Minimal custom initramfs",
        "kernel_file": "vmlinuz-virt",
        "initrd_file": "custom-initramfs.cpio.gz",
        "kernel_params": "console=ttyS0,115200 console=tty0 quiet",
        "requires_modloop": False
    }
    
    with open(custom_dir / "config.json", "w") as f:
        json.dump(custom_config, f)
    
    (custom_dir / "kernel" / "vmlinuz-virt").write_bytes(b"fake kernel")
    (custom_dir / "initrd" / "custom-initramfs.cpio.gz").write_bytes(b"fake initrd")
    
    response = admin_client.post(
        f"/api/servers/interaction/servers/{test_server.id}/boot-task",
        json={
            "boot_type": "temp_os",
            "temp_os_id": "custom-initramfs",
            "description": "Boot into custom initramfs"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["boot_type"] == "temp_os"
    assert data["temp_os_id"] == "custom-initramfs"
    
    # Custom-initramfs shouldn't have modloop in params
    assert "modloop" not in (data["kernel_params"] or "")


def test_create_boot_task_temp_os_with_additional_params(admin_client, test_server, temp_os_dir):
    """Test creating boot task with additional kernel parameters"""
    response = admin_client.post(
        f"/api/servers/interaction/servers/{test_server.id}/boot-task",
        json={
            "boot_type": "temp_os",
            "temp_os_id": "alpine",
            "kernel_params": "console=ttyS0,115200",
            "description": "Boot into Alpine Linux"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should include both default params and additional params
    assert "console=ttyS0,115200" in data["kernel_params"]
    assert "modloop=" in data["kernel_params"]


def test_get_boot_task_temp_os(admin_client, test_server, temp_os_dir, db_session):
    """Test getting a boot task with temp_os type"""
    # Create boot task directly
    boot_task = BootTaskDAO.create(
        db=db_session,
        server_id=test_server.id,
        boot_type="temp_os",
        temp_os_id="alpine",
        description="Boot into Alpine Linux"
    )
    
    response = admin_client.get(
        f"/api/servers/interaction/servers/{test_server.id}/boot-task"
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert data is not None
    assert data["boot_type"] == "temp_os"
    assert data["temp_os_id"] == "alpine"


def test_pxe_boot_script_temp_os(client, test_server, temp_os_dir, db_session, test_location, test_plugin):
    """Test PXE boot script generation for temp_os boot task"""
    from app.dao import NetworkPortDAO
    
    # Create network port with PXE enabled
    port = NetworkPortDAO.create(
        db=db_session,
        server_id=test_server.id,
        name="eth0",
        mac_address="00:0E:1E:6F:16:B0",
        speed_mbps=1000,
        pxe_boot=True
    )
    
    # Create boot task
    boot_task = BootTaskDAO.create(
        db=db_session,
        server_id=test_server.id,
        boot_type="temp_os",
        temp_os_id="alpine",
        description="Boot into Alpine Linux"
    )
    
    # Get PXE boot script
    response = client.get(
        "/api/servers/interaction/pxe",
        params={"mac": "00:0E:1E:6F:16:B0"}
    )
    
    assert response.status_code == 200
    script = response.text
    
    # Should contain iPXE script with kernel and initrd URLs
    assert "#!ipxe" in script
    assert "Booting Alpine Linux" in script
    assert "temp-os/alpine/kernel" in script
    assert "temp-os/alpine/initrd" in script
    assert "modloop=" in script
