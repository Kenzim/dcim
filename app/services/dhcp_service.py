"""
DHCP Service Manager

Manages the ISC DHCP server (dhcpd) via systemd, allowing the backend
to start, stop, restart, and reload the DHCP server. The service runs
independently via systemd, so it persists across app restarts.
"""
import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class DHCPStatus(str, Enum):
    """DHCP server status"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class DHCPService:
    """
    Manages the ISC DHCP server (dhcpd) subprocess.
    
    Handles starting, stopping, restarting, and reloading the DHCP server.
    """
    
    def __init__(
        self,
        config_path: Optional[str] = None,
        interface: Optional[str] = None,
        lease_file: Optional[str] = None,
        pid_file: Optional[str] = None
    ):
        """
        Initialize DHCP service manager.
        
        Args:
            config_path: Path to dhcpd.conf configuration file (defaults from DB config)
            interface: Network interface to bind to (defaults from DB config)
            lease_file: Path to dhcpd.leases file (defaults from DB config)
            pid_file: Optional path to PID file (defaults to /var/run/dhcpd.pid)
        """
        # These will be set from DB config when starting
        self.config_path = Path(config_path) if config_path else Path("/root/dcim/dhcpd.conf")
        self.interface = interface or "eth1"
        self.lease_file = Path(lease_file) if lease_file else Path("/root/dcim/dhcpd.leases")
        # Use a PID file in the project directory so we can manage it without root
        self.pid_file = Path(pid_file) if pid_file else Path("/root/dcim/dhcpd.pid")
        
        self.status = DHCPStatus.STOPPED
        self._lock = asyncio.Lock()
        self.service_name = "dcim-dhcpd.service"
        
        # Check if dhcpd is available
        self.dhcpd_binary = shutil.which("dhcpd")
        if not self.dhcpd_binary:
            logger.warning("dhcpd binary not found in PATH. DHCP service will not be available.")
            self.dhcpd_binary = "/usr/sbin/dhcpd"  # Common location
    
    def _load_config_from_db(self, db_session):
        """Load configuration from database and update instance variables."""
        from app.dao.dhcp_config_dao import DHCPConfigDAO
        config = DHCPConfigDAO.get_config(db_session)
        
        if config:
            self.config_path = Path(config.config_file_path)
            self.lease_file = Path(config.lease_file_path)
            # Use first interface if available
            if config.interfaces and len(config.interfaces) > 0:
                self.interface = config.interfaces[0]["interface"]
    
    async def _check_config(self) -> Tuple[bool, str]:
        """
        Validate DHCP configuration file.
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not self.config_path.exists():
            return False, f"Configuration file not found: {self.config_path}"
        
        try:
            # Use dhcpd -t to test configuration
            process = await asyncio.create_subprocess_exec(
                self.dhcpd_binary,
                "-t",  # Test mode
                "-cf", str(self.config_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=10.0
            )
            
            if process.returncode == 0:
                return True, ""
            else:
                error_msg = stderr.decode('utf-8', errors='ignore') or stdout.decode('utf-8', errors='ignore')
                return False, error_msg.strip()
                
        except asyncio.TimeoutError:
            return False, "Configuration test timed out"
        except Exception as e:
            return False, f"Failed to test configuration: {str(e)}"
    
    def _load_config_from_service(self):
        """Load configuration from DHCP config service and update instance variables."""
        try:
            from app.services.dhcp_config_service import get_dhcp_config_service
            config_service = get_dhcp_config_service()
            config = config_service.get_config()
            
            if config:
                self.config_path = Path(config.config_file_path)
                self.lease_file_path = Path(config.lease_file_path)
                # Use first interface if available (access as Pydantic model attribute, not dict)
                if config.interfaces and len(config.interfaces) > 0:
                    self.interface = config.interfaces[0].interface
        except Exception as e:
            logger.warning(f"Failed to load config from service, using defaults: {e}")
    
    def _ensure_service_file(self):
        """Ensure systemd service file is up to date."""
        from app.services.systemd_service import get_systemd_manager
        
        # Load config to get current settings
        self._load_config_from_service()
        
        systemd_manager = get_systemd_manager()
        service_content = systemd_manager.generate_dhcp_service_file(
            str(self.config_path),
            str(self.lease_file),
            str(self.pid_file),
            self.interface
        )
        
        systemd_manager.install_service(self.service_name, service_content)
    
    async def start(self) -> Dict[str, Any]:
        """
        Start the DHCP server via systemd.
        
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
                self.status = DHCPStatus.RUNNING
                return {
                    "success": True,
                    "status": "running",
                    "message": "DHCP server is already running",
                    "pid": status_info.get("pid")
                }
            
            if self.status == DHCPStatus.STARTING:
                return {
                    "success": False,
                    "status": "starting",
                    "message": "DHCP server is already starting"
                }
            
            try:
                # Validate configuration first
                is_valid, error_msg = await self._check_config()
                if not is_valid:
                    self.status = DHCPStatus.ERROR
                    return {
                        "success": False,
                        "status": "error",
                        "message": f"Configuration validation failed: {error_msg}",
                        "error": error_msg
                    }
                
                # Ensure lease file exists and is writable
                self.lease_file.parent.mkdir(parents=True, exist_ok=True)
                if not self.lease_file.exists():
                    self.lease_file.touch()
                
                self.status = DHCPStatus.STARTING
                
                # Ensure systemd service file is up to date
                self._ensure_service_file()
                
                # Start via systemd
                success, output = systemd_manager.start_service(self.service_name)
                
                if success:
                    # Get updated status
                    status_info = systemd_manager.get_service_status(self.service_name)
                    if status_info.get("active"):
                        self.status = DHCPStatus.RUNNING
                        logger.info(f"DHCP server started successfully via systemd (PID: {status_info.get('pid')})")
                        return {
                            "success": True,
                            "status": "running",
                            "message": "DHCP server started successfully",
                            "pid": status_info.get("pid")
                        }
                    else:
                        self.status = DHCPStatus.ERROR
                        return {
                            "success": False,
                            "status": "error",
                            "message": f"Failed to start DHCP server: {output}",
                            "error": output
                        }
                else:
                    self.status = DHCPStatus.ERROR
                    return {
                        "success": False,
                        "status": "error",
                        "message": f"Failed to start DHCP server: {output}",
                        "error": output
                    }
                
            except Exception as e:
                self.status = DHCPStatus.ERROR
                error_str = str(e)
                logger.error(f"Failed to start DHCP server: {error_str}", exc_info=True)
                return {
                    "success": False,
                    "status": "error",
                    "message": f"Failed to start DHCP server: {error_str}",
                    "error": error_str
                }
    
    async def stop(self) -> Dict[str, Any]:
        """
        Stop the DHCP server via systemd.
        
        Returns:
            Dict with status and message
        """
        async with self._lock:
            from app.services.systemd_service import get_systemd_manager
            
            systemd_manager = get_systemd_manager()
            
            # Check current status
            status_info = systemd_manager.get_service_status(self.service_name)
            if not status_info.get("active"):
                self.status = DHCPStatus.STOPPED
                return {
                    "success": True,
                    "status": "stopped",
                    "message": "DHCP server is already stopped"
                }
            
            if self.status == DHCPStatus.STOPPING:
                return {
                    "success": False,
                    "status": "stopping",
                    "message": "DHCP server is already stopping"
                }
            
            try:
                self.status = DHCPStatus.STOPPING
                
                logger.info(f"Stopping DHCP server via systemd")
                
                # Stop via systemd
                success, output = systemd_manager.stop_service(self.service_name)
                
                if success:
                    self.status = DHCPStatus.STOPPED
                    logger.info("DHCP server stopped successfully")
                    return {
                        "success": True,
                        "status": "stopped",
                        "message": "DHCP server stopped successfully"
                    }
                else:
                    self.status = DHCPStatus.ERROR
                    return {
                        "success": False,
                        "status": "error",
                        "message": f"Failed to stop DHCP server: {output}",
                        "error": output
                    }
                
            except Exception as e:
                error_str = str(e)
                logger.error(f"Failed to stop DHCP server: {error_str}", exc_info=True)
                self.status = DHCPStatus.ERROR
                return {
                    "success": False,
                    "status": "error",
                    "message": f"Failed to stop DHCP server: {error_str}",
                    "error": error_str
                }
    
    async def restart(self) -> Dict[str, Any]:
        """
        Restart the DHCP server.
        
        Returns:
            Dict with status and message
        """
        logger.info("Restarting DHCP server...")
        
        # Stop first (ignore errors if not running)
        await self.stop()
        
        # Wait a moment
        await asyncio.sleep(1)
        
        # Start
        return await self.start()
    
    async def reload(self) -> Dict[str, Any]:
        """
        Reload DHCP server configuration via systemd.
        
        Returns:
            Dict with status and message
        """
        logger.info("Reloading DHCP server configuration...")
        
        from app.services.systemd_service import get_systemd_manager
        
        systemd_manager = get_systemd_manager()
        
        # Check if running
        status_info = systemd_manager.get_service_status(self.service_name)
        if not status_info.get("active"):
            return {
                "success": False,
                "status": "error",
                "message": "Cannot reload: DHCP server is not running"
            }
        
        try:
            # Update service file first (in case config changed)
            self._ensure_service_file()
            
            # Reload via systemd (sends SIGHUP)
            success, output = systemd_manager.reload_service(self.service_name)
            
            if success:
                logger.info("DHCP server configuration reloaded successfully")
                status_info = systemd_manager.get_service_status(self.service_name)
                return {
                    "success": True,
                    "status": "running",
                    "message": "DHCP server configuration reloaded successfully",
                    "pid": status_info.get("pid")
                }
            else:
                # If reload fails, try restart
                logger.warning("Reload failed, attempting restart")
                return await self.restart()
        except Exception as e:
            logger.error(f"Failed to reload DHCP server: {e}")
            return {
                "success": False,
                "status": "error",
                "message": f"Failed to reload DHCP server: {str(e)}"
            }
    
    async def get_status(self) -> Dict[str, Any]:
        """
        Get current DHCP server status via systemd.
        
        Returns:
            Dict with status, pid, and other details
        """
        # Load config to get current paths
        self._load_config_from_service()
        
        from app.services.systemd_service import get_systemd_manager
        
        systemd_manager = get_systemd_manager()
        status_info = systemd_manager.get_service_status(self.service_name)
        
        is_running = status_info.get("active", False)
        if is_running:
            self.status = DHCPStatus.RUNNING
        else:
            self.status = DHCPStatus.STOPPED
        
        return {
            "status": self.status.value,
            "pid": status_info.get("pid"),
            "running": is_running,
            "config_path": str(self.config_path),
            "interface": self.interface,
            "lease_file": str(self.lease_file)
        }
    
    async def cleanup(self):
        """Cleanup resources on shutdown."""
        # Don't stop the daemon on app shutdown - let it keep running
        # The daemon runs independently and should persist
        logger.info("DHCP service cleanup: daemon will continue running independently")


# Global instance
_dhcp_service: Optional[DHCPService] = None


def get_dhcp_service() -> DHCPService:
    """Get the global DHCP service instance."""
    global _dhcp_service
    if _dhcp_service is None:
        _dhcp_service = DHCPService()
    return _dhcp_service
