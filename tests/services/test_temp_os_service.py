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
    (alpine_dir / "kernel" / "vmlinuz-virt").write_bytes(b"fake kernel")
    (alpine_dir / "initrd" / "initramfs-virt").write_bytes(b"fake initrd")
    (alpine_dir / "modloop" / "modloop-virt").write_bytes(b"fake modloop")
    
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
    
    # Create kernel and initrd files
    (custom_dir / "kernel" / "vmlinuz-virt").write_bytes(b"fake kernel")
    (custom_dir / "initrd" / "custom-initramfs.cpio.gz").write_bytes(b"fake initrd")
    
    # Create invalid config (missing modloop file)
    invalid_dir = base_dir / "invalid"
    invalid_dir.mkdir()
    (invalid_dir / "kernel").mkdir()
    (invalid_dir / "initrd").mkdir()
    (invalid_dir / "modloop").mkdir()
    
    invalid_config = {
        "id": "invalid",
        "name": "Invalid OS",
        "kernel_file": "kernel",
        "initrd_file": "initrd",
        "modloop_file": "modloop",
        "requires_modloop": True
    }
    
    with open(invalid_dir / "config.json", "w") as f:
        json.dump(invalid_config, f)
    
    (invalid_dir / "kernel" / "kernel").write_bytes(b"fake kernel")
    (invalid_dir / "initrd" / "initrd").write_bytes(b"fake initrd")
    # Missing modloop file
    
    return base_dir


def test_scan_os_configs(temp_os_dir):
    """Test scanning temporary OS configurations"""
    service = TempOSService(base_dir=temp_os_dir)
    configs = service.scan_os_configs()
    
    # Should find 2 valid configs (alpine and custom-initramfs)
    # Invalid config should be skipped due to missing modloop file
    assert len(configs) == 2
    
    config_ids = [config.id for config in configs]
    assert "alpine" in config_ids
    assert "custom-initramfs" in config_ids
    assert "invalid" not in config_ids


def test_get_os_config(temp_os_dir):
    """Test getting a specific OS configuration"""
    service = TempOSService(base_dir=temp_os_dir)
    
    # Get Alpine config
    alpine_config = service.get_os_config("alpine")
    assert alpine_config is not None
    assert alpine_config.id == "alpine"
    assert alpine_config.name == "Alpine Linux"
    assert alpine_config.requires_modloop is True
    assert alpine_config.modloop_file == "modloop-virt"
    
    # Get custom-initramfs config
    custom_config = service.get_os_config("custom-initramfs")
    assert custom_config is not None
    assert custom_config.id == "custom-initramfs"
    assert custom_config.requires_modloop is False
    
    # Get non-existent config
    assert service.get_os_config("nonexistent") is None


def test_get_kernel_url(temp_os_dir):
    """Test getting kernel URL"""
    service = TempOSService(base_dir=temp_os_dir)
    base_url = "http://192.168.12.74:8000"
    
    kernel_url = service.get_kernel_url("alpine", base_url)
    assert kernel_url == f"{base_url}/api/servers/interaction/temp-os/alpine/kernel/vmlinuz-virt"
    
    # Non-existent OS
    assert service.get_kernel_url("nonexistent", base_url) is None


def test_get_initrd_url(temp_os_dir):
    """Test getting initrd URL"""
    service = TempOSService(base_dir=temp_os_dir)
    base_url = "http://192.168.12.74:8000"
    
    initrd_url = service.get_initrd_url("alpine", base_url)
    assert initrd_url == f"{base_url}/api/servers/interaction/temp-os/alpine/initrd/initramfs-virt"
    
    # Non-existent OS
    assert service.get_initrd_url("nonexistent", base_url) is None


def test_get_modloop_url(temp_os_dir):
    """Test getting modloop URL"""
    service = TempOSService(base_dir=temp_os_dir)
    base_url = "http://192.168.12.74:8000"
    
    # Alpine requires modloop
    modloop_url = service.get_modloop_url("alpine", base_url)
    assert modloop_url == f"{base_url}/api/servers/interaction/temp-os/alpine/modloop/modloop-virt"
    
    # Custom-initramfs doesn't require modloop
    assert service.get_modloop_url("custom-initramfs", base_url) is None
    
    # Non-existent OS
    assert service.get_modloop_url("nonexistent", base_url) is None


def test_get_kernel_params(temp_os_dir):
    """Test getting kernel parameters"""
    service = TempOSService(base_dir=temp_os_dir)
    base_url = "http://192.168.12.74:8000"
    
    # Alpine should include modloop parameter
    params = service.get_kernel_params("alpine")
    assert "ip=dhcp" in params
    assert "alpine_repo" in params
    assert "modloop=" in params
    assert base_url in params
    
    # Custom-initramfs should not include modloop
    params = service.get_kernel_params("custom-initramfs")
    assert "modloop" not in params
    
    # With additional parameters
    params = service.get_kernel_params("alpine", "console=ttyS0")
    assert "console=ttyS0" in params


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
    """Test scanning configs with missing files"""
    service = TempOSService(base_dir=temp_os_dir)
    
    # Invalid config should be skipped
    configs = service.scan_os_configs()
    assert "invalid" not in [c.id for c in configs]
