"""
Tests for temporary OS API endpoints
"""
import pytest
import json
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.services.temp_os_service import TempOSService


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
    
    # Create kernel, initrd, and modloop files
    (alpine_dir / "kernel" / "vmlinuz-virt").write_bytes(b"fake kernel content")
    (alpine_dir / "initrd" / "initramfs-virt").write_bytes(b"fake initrd content")
    (alpine_dir / "modloop" / "modloop-virt").write_bytes(b"fake modloop content")
    
    # Create custom-initramfs config
    custom_dir = base_dir / "custom-initramfs"
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
        "requires_modloop": False,
        "version": "1.0.0",
        "flavor": "custom"
    }
    
    with open(custom_dir / "config.json", "w") as f:
        json.dump(custom_config, f)
    
    (custom_dir / "kernel" / "vmlinuz-virt").write_bytes(b"fake kernel content")
    (custom_dir / "initrd" / "custom-initramfs.cpio.gz").write_bytes(b"fake initrd content")
    
    # Patch the service to use our temp directory
    def get_temp_os_service():
        return TempOSService(base_dir=base_dir)
    
    monkeypatch.setattr("app.api.server_interaction.get_temp_os_service", get_temp_os_service)
    
    return base_dir


@pytest.fixture
def client():
    """Create a test client"""
    return TestClient(app)


def test_list_temp_os(client, temp_os_dir):
    """Test listing temporary OSes"""
    response = client.get("/api/servers/interaction/temp-os")
    
    assert response.status_code == 200
    data = response.json()
    
    assert isinstance(data, list)
    assert len(data) == 2
    
    os_ids = [os["id"] for os in data]
    assert "alpine" in os_ids
    assert "custom-initramfs" in os_ids
    
    # Check Alpine OS details
    alpine = next(os for os in data if os["id"] == "alpine")
    assert alpine["name"] == "Alpine Linux"
    assert alpine["requires_modloop"] is True
    assert alpine["version"] == "3.23.0"
    
    # Check custom-initramfs details
    custom = next(os for os in data if os["id"] == "custom-initramfs")
    assert custom["name"] == "Custom Initramfs"
    assert custom["requires_modloop"] is False


def test_get_temp_os_kernel(client, temp_os_dir):
    """Test getting kernel file"""
    response = client.get("/api/servers/interaction/temp-os/alpine/kernel/vmlinuz-virt")
    
    assert response.status_code == 200
    assert response.content == b"fake kernel content"
    assert response.headers["Content-Type"] == "application/octet-stream"


def test_get_temp_os_kernel_not_found(client, temp_os_dir):
    """Test getting non-existent kernel file"""
    response = client.get("/api/servers/interaction/temp-os/alpine/kernel/nonexistent")
    
    assert response.status_code == 404


def test_get_temp_os_kernel_invalid_os(client, temp_os_dir):
    """Test getting kernel for non-existent OS"""
    response = client.get("/api/servers/interaction/temp-os/nonexistent/kernel/vmlinuz-virt")
    
    assert response.status_code == 404


def test_get_temp_os_kernel_path_traversal(client, temp_os_dir):
    """Test path traversal protection"""
    response = client.get("/api/servers/interaction/temp-os/alpine/kernel/../config.json")
    
    # Path traversal should be rejected (either 400 or 404 is acceptable)
    assert response.status_code in [400, 404]


def test_get_temp_os_initrd(client, temp_os_dir):
    """Test getting initrd file"""
    response = client.get("/api/servers/interaction/temp-os/alpine/initrd/initramfs-virt")
    
    assert response.status_code == 200
    assert response.content == b"fake initrd content"
    assert response.headers["Content-Type"] == "application/octet-stream"


def test_get_temp_os_initrd_not_found(client, temp_os_dir):
    """Test getting non-existent initrd file"""
    response = client.get("/api/servers/interaction/temp-os/alpine/initrd/nonexistent")
    
    assert response.status_code == 404


def test_get_temp_os_modloop(client, temp_os_dir):
    """Test getting modloop file"""
    response = client.get("/api/servers/interaction/temp-os/alpine/modloop/modloop-virt")
    
    assert response.status_code == 200
    assert response.content == b"fake modloop content"
    assert response.headers["Content-Type"] == "application/octet-stream"


def test_get_temp_os_modloop_not_found(client, temp_os_dir):
    """Test getting non-existent modloop file"""
    response = client.get("/api/servers/interaction/temp-os/alpine/modloop/nonexistent")
    
    assert response.status_code == 404


def test_get_temp_os_modloop_no_modloop_os(client, temp_os_dir):
    """Test getting modloop for OS that doesn't have modloop directory"""
    # custom-initramfs doesn't have modloop directory
    response = client.get("/api/servers/interaction/temp-os/custom-initramfs/modloop/anything")
    
    assert response.status_code == 404
