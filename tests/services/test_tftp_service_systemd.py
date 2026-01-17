"""
Tests for TFTP service with systemd integration
"""
import pytest
from unittest.mock import Mock, patch, MagicMock

from app.services.tftp_service import TFTPService, TFTPStatus
from app.services.tftp_config_service import TFTPConfig


class TestTFTPServiceSystemd:
    """Test TFTP service systemd integration"""
    
    @pytest.fixture
    def tftp_service(self):
        """Create TFTP service instance"""
        return TFTPService()
    
    @pytest.fixture
    def mock_config(self):
        """Mock TFTP config"""
        return TFTPConfig(
            enabled=True,
            root_directory="/root/dcim/tftp",
            bind_address="192.168.12.74",
            bind_port=69,
            ipv4_only=True,
            allow_create=True,
            verbose=True
        )
    
    @patch('app.services.tftp_config_service.get_tftp_config_service')
    def test_ensure_service_file(self, mock_get_config_service, tftp_service, mock_config):
        """Test service file generation"""
        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value = mock_config
        mock_get_config_service.return_value = mock_config_service
        
        with patch('app.services.systemd_service.get_systemd_manager') as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.generate_tftp_service_file.return_value = "[Unit]\nDescription=Test\n"
            mock_manager.install_service.return_value = True
            mock_get_manager.return_value = mock_manager
            
            tftp_service._load_config_from_service()
            tftp_service._ensure_service_file()
            
            mock_manager.generate_tftp_service_file.assert_called_once()
            assert mock_manager.generate_tftp_service_file.call_args[0][0] == "/root/dcim/tftp"
            assert mock_manager.generate_tftp_service_file.call_args[0][1] == "192.168.12.74"
            assert mock_manager.generate_tftp_service_file.call_args[0][2] == 69
            assert mock_manager.generate_tftp_service_file.call_args[0][3] is True  # ipv4_only
            assert mock_manager.generate_tftp_service_file.call_args[0][4] is True  # allow_create
            assert mock_manager.generate_tftp_service_file.call_args[0][5] is True  # verbose
            mock_manager.install_service.assert_called_once()
    
    @pytest.mark.asyncio
    @patch('app.services.systemd_service.get_systemd_manager')
    @patch('app.services.tftp_config_service.get_tftp_config_service')
    async def test_start_service_already_running(self, mock_get_config, mock_get_manager, tftp_service, mock_config):
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
        
        result = await tftp_service.start()
        
        assert result["success"] is True
        assert result["status"] == "running"
        assert result["pid"] == 12345
    
    @pytest.mark.asyncio
    @patch('app.services.systemd_service.get_systemd_manager')
    @patch('app.services.tftp_config_service.get_tftp_config_service')
    async def test_start_service_success(self, mock_get_config, mock_get_manager, tftp_service, mock_config):
        """Test successful service start"""
        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value = mock_config
        mock_get_config.return_value = mock_config_service
        
        mock_manager = MagicMock()
        mock_manager.get_service_status.side_effect = [
            {"active": False, "pid": None},  # Initial check
            {"active": True, "pid": 12345}  # After start
        ]
        mock_manager.start_service.return_value = (True, "Started")
        mock_get_manager.return_value = mock_manager
        
        # Mock root directory
        with patch('pathlib.Path.mkdir'):
            result = await tftp_service.start()
        
        assert result["success"] is True
        assert result["status"] == "running"
        mock_manager.start_service.assert_called_once_with("dcim-tftpd.service")
    
    @pytest.mark.asyncio
    @patch('app.services.systemd_service.get_systemd_manager')
    async def test_stop_service_already_stopped(self, mock_get_manager, tftp_service):
        """Test stopping service that's already stopped"""
        mock_manager = MagicMock()
        mock_manager.get_service_status.return_value = {
            "active": False
        }
        mock_get_manager.return_value = mock_manager
        
        result = await tftp_service.stop()
        
        assert result["success"] is True
        assert result["status"] == "stopped"
        mock_manager.stop_service.assert_not_called()
    
    @pytest.mark.asyncio
    @patch('app.services.systemd_service.get_systemd_manager')
    async def test_stop_service_success(self, mock_get_manager, tftp_service):
        """Test successful service stop"""
        mock_manager = MagicMock()
        mock_manager.get_service_status.side_effect = [
            {"active": True, "pid": 12345},  # Initial check
            {"active": False}  # After stop
        ]
        mock_manager.stop_service.return_value = (True, "Stopped")
        mock_get_manager.return_value = mock_manager
        
        result = await tftp_service.stop()
        
        assert result["success"] is True
        assert result["status"] == "stopped"
        mock_manager.stop_service.assert_called_once_with("dcim-tftpd.service")
    
    @pytest.mark.asyncio
    @patch('app.services.systemd_service.get_systemd_manager')
    async def test_get_status_running(self, mock_get_manager, tftp_service):
        """Test getting status of running service"""
        mock_manager = MagicMock()
        mock_manager.get_service_status.return_value = {
            "active": True,
            "pid": 12345
        }
        mock_get_manager.return_value = mock_manager
        
        with patch.object(tftp_service, '_load_config_from_service'):
            result = await tftp_service.get_status()
        
        assert result["status"] == "running"
        assert result["running"] is True
        assert result["pid"] == 12345
    
    @pytest.mark.asyncio
    async def test_restart_service(self, tftp_service):
        """Test restarting service"""
        with patch.object(tftp_service, 'stop', return_value={"success": True, "status": "stopped"}):
            with patch.object(tftp_service, 'start', return_value={"success": True, "status": "running"}):
                result = await tftp_service.restart()
        
        assert result["success"] is True
        assert result["status"] == "running"
