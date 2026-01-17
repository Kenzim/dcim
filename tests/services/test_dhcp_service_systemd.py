"""
Tests for DHCP service with systemd integration
"""
import pytest
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from pathlib import Path

from app.services.dhcp_service import DHCPService, DHCPStatus
from app.services.dhcp_config_service import DHCPConfig, DHCPInterfaceConfig


class TestDHCPServiceSystemd:
    """Test DHCP service systemd integration"""
    
    @pytest.fixture
    def dhcp_service(self):
        """Create DHCP service instance"""
        return DHCPService()
    
    @pytest.fixture
    def mock_config(self):
        """Mock DHCP config"""
        return DHCPConfig(
            enabled=True,
            interfaces=[DHCPInterfaceConfig(interface="eth1", ip="192.168.12.74")],
            config_file_path="/root/dcim/dhcpd.conf",
            lease_file_path="/root/dcim/dhcpd.leases"
        )
    
    @patch('app.services.dhcp_config_service.get_dhcp_config_service')
    def test_ensure_service_file(self, mock_get_config_service, dhcp_service, mock_config):
        """Test service file generation"""
        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value = mock_config
        mock_get_config_service.return_value = mock_config_service
        
        with patch('app.services.systemd_service.get_systemd_manager') as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.generate_dhcp_service_file.return_value = "[Unit]\nDescription=Test\n"
            mock_manager.install_service.return_value = True
            mock_get_manager.return_value = mock_manager
            
            dhcp_service._load_config_from_service()
            dhcp_service._ensure_service_file()
            
            mock_manager.generate_dhcp_service_file.assert_called_once()
            mock_manager.install_service.assert_called_once()
    
    @pytest.mark.asyncio
    @patch('app.services.systemd_service.get_systemd_manager')
    @patch('app.services.dhcp_config_service.get_dhcp_config_service')
    async def test_start_service_already_running(self, mock_get_config, mock_get_manager, dhcp_service, mock_config):
        """Test starting service that's already running"""
        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value = mock_config
        mock_get_config.return_value = mock_config_service
        
        mock_manager = MagicMock()
        mock_manager.get_service_status.return_value = {
            "active": True,
            "pid": 12345
        }
        mock_get_manager.return_value = mock_manager
        
        result = await dhcp_service.start()
        
        assert result["success"] is True
        assert result["status"] == "running"
        assert result["pid"] == 12345
    
    @pytest.mark.asyncio
    @patch('app.services.systemd_service.get_systemd_manager')
    @patch('app.services.dhcp_config_service.get_dhcp_config_service')
    @patch.object(DHCPService, '_check_config')
    async def test_start_service_success(self, mock_check_config, mock_get_config, mock_get_manager, dhcp_service, mock_config):
        """Test successful service start"""
        mock_check_config.return_value = (True, "")
        
        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value = mock_config
        mock_get_config.return_value = mock_config_service
        
        mock_manager = MagicMock()
        mock_manager.get_service_status.return_value = {
            "active": False,
            "pid": None
        }
        mock_manager.start_service.return_value = (True, "Started")
        mock_manager.get_service_status.side_effect = [
            {"active": False},  # Initial check
            {"active": True, "pid": 12345}  # After start
        ]
        mock_get_manager.return_value = mock_manager
        
        # Mock lease file
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pathlib.Path.touch'):
                result = await dhcp_service.start()
        
        assert result["success"] is True
        assert result["status"] == "running"
        mock_manager.start_service.assert_called_once_with("dcim-dhcpd.service")
    
    @pytest.mark.asyncio
    @patch('app.services.systemd_service.get_systemd_manager')
    async def test_stop_service_already_stopped(self, mock_get_manager, dhcp_service):
        """Test stopping service that's already stopped"""
        mock_manager = MagicMock()
        mock_manager.get_service_status.return_value = {
            "active": False
        }
        mock_get_manager.return_value = mock_manager
        
        result = await dhcp_service.stop()
        
        assert result["success"] is True
        assert result["status"] == "stopped"
        mock_manager.stop_service.assert_not_called()
    
    @pytest.mark.asyncio
    @patch('app.services.systemd_service.get_systemd_manager')
    async def test_stop_service_success(self, mock_get_manager, dhcp_service):
        """Test successful service stop"""
        mock_manager = MagicMock()
        mock_manager.get_service_status.side_effect = [
            {"active": True, "pid": 12345},  # Initial check
            {"active": False}  # After stop
        ]
        mock_manager.stop_service.return_value = (True, "Stopped")
        mock_get_manager.return_value = mock_manager
        
        result = await dhcp_service.stop()
        
        assert result["success"] is True
        assert result["status"] == "stopped"
        mock_manager.stop_service.assert_called_once_with("dcim-dhcpd.service")
    
    @pytest.mark.asyncio
    @patch('app.services.systemd_service.get_systemd_manager')
    async def test_get_status_running(self, mock_get_manager, dhcp_service):
        """Test getting status of running service"""
        mock_manager = MagicMock()
        mock_manager.get_service_status.return_value = {
            "active": True,
            "pid": 12345
        }
        mock_get_manager.return_value = mock_manager
        
        with patch.object(dhcp_service, '_load_config_from_service'):
            result = await dhcp_service.get_status()
        
        assert result["status"] == "running"
        assert result["running"] is True
        assert result["pid"] == 12345
    
    @pytest.mark.asyncio
    @patch('app.services.systemd_service.get_systemd_manager')
    async def test_reload_service(self, mock_get_manager, dhcp_service):
        """Test reloading service"""
        mock_manager = MagicMock()
        mock_manager.get_service_status.return_value = {
            "active": True,
            "pid": 12345
        }
        mock_manager.reload_service.return_value = (True, "Reloaded")
        mock_get_manager.return_value = mock_manager
        
        with patch.object(dhcp_service, '_ensure_service_file'):
            result = await dhcp_service.reload()
        
        assert result["success"] is True
        assert result["status"] == "running"
        mock_manager.reload_service.assert_called_once_with("dcim-dhcpd.service")
