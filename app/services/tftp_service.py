"""
TFTP Service Manager

Manages the TFTP server (in.tftpd) via systemd, allowing the backend
to start, stop, restart, and reload the TFTP server. The service runs
independently via systemd, so it persists across app restarts.
"""
import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class TFTPStatus(str, Enum):
    """TFTP server status"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class TFTPService:
    """
    Manages the TFTP server (in.tftpd) subprocess.
    
    Handles starting, stopping, restarting, and reloading the TFTP server.
    """
    
    def __init__(
        self,
        root_directory: Optional[str] = None,
        bind_address: Optional[str] = None,
        bind_port: Optional[int] = None
    ):
        """
        Initialize TFTP service manager.
        
        Args:
            root_directory: TFTP root directory (defaults from config)
            bind_address: IP address to bind to (defaults from config)
            bind_port: Port to bind to (defaults from config)
        """
        # These will be set from config when starting
        self.root_directory = Path(root_directory) if root_directory else Path("/root/dcim/tftp")
        self.bind_address = bind_address or "192.168.12.74"
        self.bind_port = bind_port or 69
        
        self.status = TFTPStatus.STOPPED
        self._lock = asyncio.Lock()
        self.service_name = "dcim-tftpd.service"
        
        # Check if in.tftpd is available
        self.tftpd_binary = shutil.which("in.tftpd")
        if not self.tftpd_binary:
            logger.warning("in.tftpd binary not found in PATH. TFTP service will not be available.")
            self.tftpd_binary = "/usr/sbin/in.tftpd"  # Common location
    
    def _load_config_from_service(self):
        """Load configuration from TFTP config service and update instance variables."""
        try:
            from app.services.tftp_config_service import get_tftp_config_service
            config_service = get_tftp_config_service()
            config = config_service.get_config()
            
            if config:
                self.root_directory = Path(config.root_directory)
                self.bind_address = config.bind_address
                self.bind_port = config.bind_port
        except Exception as e:
            logger.warning(f"Failed to load config from service, using defaults: {e}")
    
    def _ensure_service_file(self):
        """Ensure systemd service file is up to date."""
        from app.services.systemd_service import get_systemd_manager
        
        # Load config to get current settings
        self._load_config_from_service()
        
        # Load full config
        from app.services.tftp_config_service import get_tftp_config_service
        config_service = get_tftp_config_service()
        config = config_service.get_config()
        
        systemd_manager = get_systemd_manager()
        service_content = systemd_manager.generate_tftp_service_file(
            str(self.root_directory),
            self.bind_address,
            self.bind_port,
            config.ipv4_only,
            config.allow_create,
            config.verbose
        )
        
        systemd_manager.install_service(self.service_name, service_content)
    
    async def start(self) -> Dict[str, Any]:
        """
        Start the TFTP server via systemd.
        
        Returns:
            Dict with status, message, and details
        """
        # Load config from service before starting
        self._load_config_from_service()
        
        async with self._lock:
            from app.services.systemd_service import get_systemd_manager
            
            systemd_manager = get_systemd_manager()
            
            # Check current status
            status_info = systemd_manager.get_service_status(self.service_name)
            if status_info.get("active"):
                self.status = TFTPStatus.RUNNING
                return {
                    "success": True,
                    "status": "running",
                    "message": "TFTP server is already running",
                    "pid": status_info.get("pid")
                }
            
            if self.status == TFTPStatus.STARTING:
                return {
                    "success": False,
                    "status": "starting",
                    "message": "TFTP server is already starting"
                }
            
            try:
                # Ensure root directory exists
                self.root_directory.mkdir(parents=True, exist_ok=True)
                
                self.status = TFTPStatus.STARTING
                
                # Ensure systemd service file is up to date
                self._ensure_service_file()
                
                # Start via systemd
                success, output = systemd_manager.start_service(self.service_name)
                
                if success:
                    # Get updated status
                    status_info = systemd_manager.get_service_status(self.service_name)
                    if status_info.get("active"):
                        self.status = TFTPStatus.RUNNING
                        logger.info(f"TFTP server started successfully via systemd (PID: {status_info.get('pid')})")
                        return {
                            "success": True,
                            "status": "running",
                            "message": "TFTP server started successfully",
                            "pid": status_info.get("pid")
                        }
                    else:
                        self.status = TFTPStatus.ERROR
                        return {
                            "success": False,
                            "status": "error",
                            "message": f"Failed to start TFTP server: {output}",
                            "error": output
                        }
                else:
                    self.status = TFTPStatus.ERROR
                    return {
                        "success": False,
                        "status": "error",
                        "message": f"Failed to start TFTP server: {output}",
                        "error": output
                    }
                
            except Exception as e:
                self.status = TFTPStatus.ERROR
                error_str = str(e)
                logger.error(f"Failed to start TFTP server: {error_str}", exc_info=True)
                return {
                    "success": False,
                    "status": "error",
                    "message": f"Failed to start TFTP server: {error_str}",
                    "error": error_str
                }
    
    async def stop(self) -> Dict[str, Any]:
        """
        Stop the TFTP server via systemd.
        
        Returns:
            Dict with status and message
        """
        async with self._lock:
            from app.services.systemd_service import get_systemd_manager
            
            systemd_manager = get_systemd_manager()
            
            # Check current status
            status_info = systemd_manager.get_service_status(self.service_name)
            if not status_info.get("active"):
                self.status = TFTPStatus.STOPPED
                return {
                    "success": True,
                    "status": "stopped",
                    "message": "TFTP server is already stopped"
                }
            
            if self.status == TFTPStatus.STOPPING:
                return {
                    "success": False,
                    "status": "stopping",
                    "message": "TFTP server is already stopping"
                }
            
            try:
                self.status = TFTPStatus.STOPPING
                
                logger.info(f"Stopping TFTP server via systemd")
                
                # Stop via systemd
                success, output = systemd_manager.stop_service(self.service_name)
                
                if success:
                    self.status = TFTPStatus.STOPPED
                    logger.info("TFTP server stopped successfully")
                    return {
                        "success": True,
                        "status": "stopped",
                        "message": "TFTP server stopped successfully"
                    }
                else:
                    self.status = TFTPStatus.ERROR
                    return {
                        "success": False,
                        "status": "error",
                        "message": f"Failed to stop TFTP server: {output}",
                        "error": output
                    }
                
            except Exception as e:
                error_str = str(e)
                logger.error(f"Failed to stop TFTP server: {error_str}", exc_info=True)
                self.status = TFTPStatus.ERROR
                return {
                    "success": False,
                    "status": "error",
                    "message": f"Failed to stop TFTP server: {error_str}",
                    "error": error_str
                }
    
    async def restart(self) -> Dict[str, Any]:
        """
        Restart the TFTP server.
        
        Returns:
            Dict with status and message
        """
        logger.info("Restarting TFTP server...")
        
        # Stop first (ignore errors if not running)
        await self.stop()
        
        # Wait a moment
        await asyncio.sleep(1)
        
        # Start
        return await self.start()
    
    async def reload(self) -> Dict[str, Any]:
        """
        Reload TFTP server configuration.
        
        Note: TFTP doesn't support graceful reload, so this restarts the server.
        
        Returns:
            Dict with status and message
        """
        logger.info("Reloading TFTP server configuration...")
        return await self.restart()
    
    async def get_status(self) -> Dict[str, Any]:
        """
        Get current TFTP server status via systemd.
        
        Returns:
            Dict with status, pid, and other details
        """
        # Load config to get current settings
        self._load_config_from_service()
        
        from app.services.systemd_service import get_systemd_manager
        
        systemd_manager = get_systemd_manager()
        status_info = systemd_manager.get_service_status(self.service_name)
        
        is_running = status_info.get("active", False)
        if is_running:
            self.status = TFTPStatus.RUNNING
        else:
            self.status = TFTPStatus.STOPPED
        
        return {
            "status": self.status.value,
            "pid": status_info.get("pid"),
            "running": is_running,
            "root_directory": str(self.root_directory),
            "bind_address": self.bind_address,
            "bind_port": self.bind_port
        }
    
    async def cleanup(self):
        """Cleanup resources on shutdown."""
        # Don't stop the daemon on app shutdown - let it keep running
        # The daemon runs independently and should persist
        logger.info("TFTP service cleanup: daemon will continue running independently")


# Global instance
_tftp_service: Optional[TFTPService] = None


def get_tftp_service() -> TFTPService:
    """Get the global TFTP service instance."""
    global _tftp_service
    if _tftp_service is None:
        _tftp_service = TFTPService()
    return _tftp_service
