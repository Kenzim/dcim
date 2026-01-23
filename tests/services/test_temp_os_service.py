"""
Tests for temporary OS service
"""
import pytest
import json
import tempfile
import shutil
from pathlib import Path
from app.services.temp_os_service import TempOSService, TempOSConfig


@pytest.fixture
def temp_os_dir(tmp_path):
    """Create a temporary directory structure for temp OSes"""
    base_dir = tmp_path / "temp_os"
    base_dir.mkdir(parents=True)
    
    # Create Alpine config
    alpine_dir = base_dir / "alpine"
    alpine_dir.mkdir()
    
    alpine_config = {
        "id": "alpine",
        "name": "Alpine Linux",
        "description": "Lightweight Linux distribution",
        "kernel_file": "vmlinuz-virt",
        "initrd_file": "initramfs-virt",
        "kernel_params": "ip=dhcp alpine_repo=http://dl-cdn.alpinelinux.org/alpine/latest-stable/main",
        "version": "3.23.0",
        "flavor": "virt"
    }
    
    with open(alpine_dir / "config.json", "w") as f:
        json.dump(alpine_config, f)
    
    # Create custom-initramfs config
    custom_dir = base_dir / "custom-initramfs"
    custom_dir.mkdir()
    
    custom_config = {
        "id": "custom-initramfs",
        "name": "Custom Initramfs",
        "description": "Minimal custom initramfs",
        "kernel_file": "vmlinuz-virt",
        "initrd_file": "custom-initramfs.cpio.gz",
        "kernel_params": "console=ttyS0,115200 console=tty0 quiet",
        "version": "1.0.0",
        "flavor": "custom"
    }
    
    with open(custom_dir / "config.json", "w") as f:
        json.dump(custom_config, f)
    
    # Create invalid config (missing required fields)
    invalid_dir = base_dir / "invalid"
    invalid_dir.mkdir()
    
    invalid_config = {
        "id": "invalid",
        "name": "Invalid OS",
        "kernel_file": "kernel",
        "initrd_file": "initrd"
        # Missing description is OK, but config is still valid
    }
    
    with open(invalid_dir / "config.json", "w") as f:
        json.dump(invalid_config, f)
    
    return base_dir


def test_scan_os_configs(temp_os_dir):
    """Test scanning temporary OS configurations"""
    service = TempOSService(base_dir=temp_os_dir)
    configs = service.scan_os_configs()
    
    # Should find all 3 configs (alpine, custom-initramfs, and invalid)
    # Service no longer validates file existence, only config structure
    assert len(configs) == 3
    
    config_ids = [config.id for config in configs]
    assert "alpine" in config_ids
    assert "custom-initramfs" in config_ids
    assert "invalid" in config_ids


def test_get_os_config(temp_os_dir):
    """Test getting a specific OS configuration"""
    service = TempOSService(base_dir=temp_os_dir)
    
    # Get Alpine config
    alpine_config = service.get_os_config("alpine")
    assert alpine_config is not None
    assert alpine_config.id == "alpine"
    assert alpine_config.name == "Alpine Linux"
    assert alpine_config.kernel_file == "vmlinuz-virt"
    assert alpine_config.initrd_file == "initramfs-virt"
    
    # Get custom-initramfs config
    custom_config = service.get_os_config("custom-initramfs")
    assert custom_config is not None
    assert custom_config.id == "custom-initramfs"
    assert custom_config.kernel_file == "vmlinuz-virt"
    assert custom_config.initrd_file == "custom-initramfs.cpio.gz"
    
    # Get non-existent config
    assert service.get_os_config("nonexistent") is None


def test_get_kernel_url(temp_os_dir):
    """Test getting kernel URL"""
    service = TempOSService(base_dir=temp_os_dir)
    base_url = "http://192.168.12.74:8000"
    
    kernel_url = service.get_kernel_url("alpine", base_url)
    assert kernel_url == f"{base_url}/api/servers/interaction/temp-os/alpine/files/vmlinuz-virt"
    
    # Non-existent OS
    assert service.get_kernel_url("nonexistent", base_url) is None


def test_get_initrd_url(temp_os_dir):
    """Test getting initrd URL"""
    service = TempOSService(base_dir=temp_os_dir)
    base_url = "http://192.168.12.74:8000"
    
    initrd_url = service.get_initrd_url("alpine", base_url)
    assert initrd_url == f"{base_url}/api/servers/interaction/temp-os/alpine/files/initramfs-virt"
    
    # Non-existent OS
    assert service.get_initrd_url("nonexistent", base_url) is None


def test_get_kernel_params(temp_os_dir):
    """Test getting kernel parameters"""
    service = TempOSService(base_dir=temp_os_dir)
    
    # Alpine kernel params
    params = service.get_kernel_params("alpine")
    assert "ip=dhcp" in params
    assert "alpine_repo" in params
    
    # Custom-initramfs kernel params
    params = service.get_kernel_params("custom-initramfs")
    assert "console=ttyS0,115200" in params
    assert "console=tty0" in params
    assert "quiet" in params
    
    # With additional parameters
    params = service.get_kernel_params("alpine", "console=ttyS0")
    assert "console=ttyS0" in params
    assert "ip=dhcp" in params


def test_get_os_dir(temp_os_dir):
    """Test getting OS directory"""
    service = TempOSService(base_dir=temp_os_dir)
    
    alpine_dir = service.get_os_dir("alpine")
    assert alpine_dir is not None
    assert alpine_dir.name == "alpine"
    assert (alpine_dir / "config.json").exists()
    
    # Non-existent OS
    assert service.get_os_dir("nonexistent") is None


def test_temp_os_config_validation():
    """Test TempOSConfig validation"""
    # Valid config
    config = TempOSConfig(
        id="test",
        name="Test OS",
        kernel_file="kernel",
        initrd_file="initrd"
    )
    assert config.id == "test"
    
    # Invalid ID (contains invalid characters)
    with pytest.raises(ValueError):
        TempOSConfig(
            id="test@invalid",
            name="Test OS",
            kernel_file="kernel",
            initrd_file="initrd"
        )


def test_scan_empty_directory(tmp_path):
    """Test scanning an empty directory"""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    
    service = TempOSService(base_dir=empty_dir)
    configs = service.scan_os_configs()
    
    assert len(configs) == 0


def test_scan_missing_files(temp_os_dir):
    """Test scanning configs - service validates config structure, not file existence"""
    service = TempOSService(base_dir=temp_os_dir)
    
    # All configs with valid structure are included
    # Service no longer checks for file existence
    configs = service.scan_os_configs()
    config_ids = [c.id for c in configs]
    assert "invalid" in config_ids  # Invalid config is included if structure is valid
