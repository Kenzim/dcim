"""
Tests for systemd service management
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import subprocess

from app.services.systemd_service import SystemdServiceManager


class TestSystemdServiceManager:
    """Test SystemdServiceManager"""
    
    def test_init(self):
        """Test initialization"""
        manager = SystemdServiceManager()
        assert manager is not None
        assert Path("/root/dcim/systemd").exists()
    
    @patch('subprocess.run')
    def test_reload_systemd_success(self, mock_run):
        """Test successful systemd daemon reload"""
        mock_run.return_value = Mock(returncode=0)
        manager = SystemdServiceManager()
        result = manager._reload_systemd()
        assert result is True
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["systemctl", "daemon-reload"]
    
    @patch('subprocess.run')
    def test_reload_systemd_failure(self, mock_run):
        """Test failed systemd daemon reload"""
        mock_run.return_value = Mock(returncode=1)
        manager = SystemdServiceManager()
        result = manager._reload_systemd()
        assert result is False
    
    def test_generate_dhcp_service_file(self):
        """Test DHCP service file generation"""
        manager = SystemdServiceManager()
        content = manager.generate_dhcp_service_file(
            "/root/dcim/dhcpd.conf",
            "/root/dcim/dhcpd.leases",
            "/root/dcim/dhcpd.pid",
            "eth1"
        )
        
        assert "[Unit]" in content
        assert "Description=DCIM DHCP Server" in content
        assert "Type=forking" in content
        assert "/usr/sbin/dhcpd" in content
        assert "-cf /root/dcim/dhcpd.conf" in content
        assert "-lf /root/dcim/dhcpd.leases" in content
        assert "-pf /root/dcim/dhcpd.pid" in content
        assert "eth1" in content
        assert "PIDFile=/root/dcim/dhcpd.pid" in content
    
    def test_generate_tftp_service_file(self):
        """Test TFTP service file generation"""
        manager = SystemdServiceManager()
        content = manager.generate_tftp_service_file(
            "/root/dcim/tftp",
            "192.168.12.74",
            69,
            True,  # ipv4_only
            True,  # allow_create
            True   # verbose
        )
        
        assert "[Unit]" in content
        assert "Description=DCIM TFTP Server" in content
        assert "Type=simple" in content
        assert "/usr/sbin/in.tftpd" in content
        assert "-4" in content
        assert "-L" in content  # Foreground mode
        assert "-s /root/dcim/tftp" in content
        assert "-a 192.168.12.74:69" in content
        assert "-c" in content  # allow_create
        assert "-v" in content  # verbose
    
    def test_generate_tftp_service_file_minimal(self):
        """Test TFTP service file generation with minimal options"""
        manager = SystemdServiceManager()
        content = manager.generate_tftp_service_file(
            "/root/dcim/tftp",
            "192.168.12.74",
            69,
            False,  # ipv4_only
            False,  # allow_create
            False   # verbose
        )
        
        assert "-4" not in content
        assert "-c" not in content
        assert "-v" not in content
        assert "-L" in content  # Always use foreground mode
    
    @patch('subprocess.run')
    @patch('pathlib.Path.write_text')
    def test_install_service_success(self, mock_write, mock_run):
        """Test successful service installation"""
        mock_run.return_value = Mock(returncode=0)
        mock_write.return_value = None
        
        manager = SystemdServiceManager()
        service_content = "[Unit]\nDescription=Test Service\n"
        result = manager.install_service("test.service", service_content)
        
        assert result is True
        mock_write.assert_called_once()
        assert mock_run.call_count == 1  # daemon-reload
    
    @patch('subprocess.run')
    @patch('pathlib.Path.write_text')
    def test_install_service_failure(self, mock_write, mock_run):
        """Test failed service installation"""
        mock_run.return_value = Mock(returncode=1)  # daemon-reload fails
        mock_write.return_value = None
        
        manager = SystemdServiceManager()
        service_content = "[Unit]\nDescription=Test Service\n"
        result = manager.install_service("test.service", service_content)
        
        assert result is False
    
    @patch('subprocess.run')
    def test_start_service_success(self, mock_run):
        """Test successful service start"""
        mock_run.return_value = Mock(returncode=0, stdout="Service started")
        manager = SystemdServiceManager()
        success, output = manager.start_service("test.service")
        
        assert success is True
        assert "Service started" in output
        mock_run.assert_called_once_with(
            ["systemctl", "start", "test.service"],
            capture_output=True,
            text=True,
            timeout=10
        )
    
    @patch('subprocess.run')
    def test_start_service_failure(self, mock_run):
        """Test failed service start"""
        mock_run.return_value = Mock(returncode=1, stderr="Failed to start")
        manager = SystemdServiceManager()
        success, output = manager.start_service("test.service")
        
        assert success is False
        assert "Failed to start" in output
    
    @patch('subprocess.run')
    def test_stop_service(self, mock_run):
        """Test service stop"""
        mock_run.return_value = Mock(returncode=0, stdout="Service stopped")
        manager = SystemdServiceManager()
        success, output = manager.stop_service("test.service")
        
        assert success is True
        mock_run.assert_called_once_with(
            ["systemctl", "stop", "test.service"],
            capture_output=True,
            text=True,
            timeout=10
        )
    
    @patch('subprocess.run')
    def test_restart_service(self, mock_run):
        """Test service restart"""
        mock_run.return_value = Mock(returncode=0, stdout="Service restarted")
        manager = SystemdServiceManager()
        success, output = manager.restart_service("test.service")
        
        assert success is True
        mock_run.assert_called_once_with(
            ["systemctl", "restart", "test.service"],
            capture_output=True,
            text=True,
            timeout=10
        )
    
    @patch('subprocess.run')
    def test_get_service_status_active(self, mock_run):
        """Test getting status of active service"""
        # Mock systemctl calls - status check must succeed first
        def mock_systemctl(*args, **kwargs):
            cmd = args[0]
            if len(cmd) > 1:
                if cmd[1] == "status":
                    return Mock(returncode=0, stdout="Active: active")  # Status check succeeds
                elif cmd[1] == "is-active":
                    return Mock(returncode=0, stdout="")
                elif cmd[1] == "is-enabled":
                    return Mock(returncode=0, stdout="enabled")
            # Handle subprocess.run for PID check - it's a separate subprocess.run call
            # The PID check uses: ["systemctl", "show", service_name, "--property=MainPID", "--value"]
            if len(cmd) > 2 and "--property=MainPID" in cmd:
                return Mock(returncode=0, stdout="12345")
            return Mock(returncode=1, stderr="Error")
        
        mock_run.side_effect = mock_systemctl
        
        manager = SystemdServiceManager()
        status = manager.get_service_status("test.service")
        
        assert status["active"] is True
        assert status.get("enabled") is True  # Use .get() since it's only present when status check succeeds
        assert status["status"] == "running"
        assert status["pid"] == 12345
    
    @patch('subprocess.run')
    def test_get_service_status_inactive(self, mock_run):
        """Test getting status of inactive service"""
        def mock_systemctl(*args, **kwargs):
            if args[0][1] == "is-active":
                return Mock(returncode=1, stdout="")  # Not active
            return Mock(returncode=1, stderr="Error")
        
        mock_run.side_effect = mock_systemctl
        
        manager = SystemdServiceManager()
        status = manager.get_service_status("test.service")
        
        assert status["active"] is False
        assert status["status"] == "stopped"
