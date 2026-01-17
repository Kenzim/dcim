"""
Systemd Service Manager

Manages systemd service files for DHCP and TFTP servers.
Generates and updates service files dynamically based on configuration.
"""
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

SYSTEMD_SERVICE_DIR = Path("/etc/systemd/system")
DCIM_SERVICE_DIR = Path("/root/dcim/systemd")


class SystemdServiceManager:
    """Manages systemd service files and operations"""
    
    def __init__(self):
        # Ensure our service directory exists
        DCIM_SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    
    def _run_systemctl(self, command: str, service_name: str) -> tuple[bool, str]:
        """
        Run a systemctl command.
        
        Args:
            command: systemctl command (start, stop, restart, reload, status, enable, disable)
            service_name: Name of the service
            
        Returns:
            Tuple of (success, output)
        """
        try:
            result = subprocess.run(
                ["systemctl", command, service_name],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return True, result.stdout
            else:
                return False, result.stderr or result.stdout
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)
    
    def _reload_systemd(self) -> bool:
        """Reload systemd daemon to pick up new service files."""
        try:
            result = subprocess.run(
                ["systemctl", "daemon-reload"],
                capture_output=True,
                timeout=10
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Failed to reload systemd: {e}")
            return False
    
    def generate_dhcp_service_file(self, config_path: str, lease_file: str, pid_file: str, interface: str) -> str:
        """Generate systemd service file content for DHCP."""
        return f"""[Unit]
Description=DCIM DHCP Server
After=network.target

[Service]
Type=forking
PIDFile={pid_file}
ExecStart=/usr/sbin/dhcpd -q -cf {config_path} -lf {lease_file} -pf {pid_file} {interface}
ExecReload=/bin/kill -HUP $MAINPID
ExecStop=/bin/kill -TERM $MAINPID
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    
    def generate_tftp_service_file(self, root_directory: str, bind_address: str, bind_port: int, 
                                   ipv4_only: bool, allow_create: bool, verbose: bool) -> str:
        """Generate systemd service file content for TFTP."""
        cmd_parts = ["/usr/sbin/in.tftpd"]
        if ipv4_only:
            cmd_parts.append("-4")
        cmd_parts.append("-L")  # Foreground mode (don't detach) - required for systemd Type=simple
        cmd_parts.extend(["-s", root_directory])  # Secure/chroot mode
        cmd_parts.extend(["-a", f"{bind_address}:{bind_port}"])  # Bind address:port
        if allow_create:
            cmd_parts.append("-c")  # Allow file creation
        if verbose:
            cmd_parts.append("-v")  # Verbose
        
        # Use Type=simple since -L keeps it in foreground
        cmd = " ".join(cmd_parts)
        
        return f"""[Unit]
Description=DCIM TFTP Server
After=network.target

[Service]
Type=simple
ExecStart={cmd}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    
    def install_service(self, service_name: str, service_content: str) -> bool:
        """
        Install a systemd service file.
        
        Args:
            service_name: Name of the service (e.g., dcim-dhcpd.service)
            service_content: Content of the service file
            
        Returns:
            True if successful
        """
        try:
            service_file = SYSTEMD_SERVICE_DIR / service_name
            
            # Write service file
            service_file.write_text(service_content)
            logger.info(f"Created systemd service file: {service_file}")
            
            # Reload systemd
            if not self._reload_systemd():
                logger.error("Failed to reload systemd daemon")
                return False
            
            return True
        except Exception as e:
            logger.error(f"Failed to install service {service_name}: {e}")
            return False
    
    def start_service(self, service_name: str) -> tuple[bool, str]:
        """Start a systemd service."""
        return self._run_systemctl("start", service_name)
    
    def stop_service(self, service_name: str) -> tuple[bool, str]:
        """Stop a systemd service."""
        return self._run_systemctl("stop", service_name)
    
    def restart_service(self, service_name: str) -> tuple[bool, str]:
        """Restart a systemd service."""
        return self._run_systemctl("restart", service_name)
    
    def reload_service(self, service_name: str) -> tuple[bool, str]:
        """Reload a systemd service (if supported)."""
        return self._run_systemctl("reload", service_name)
    
    def get_service_status(self, service_name: str) -> Dict[str, Any]:
        """
        Get status of a systemd service.
        
        Returns:
            Dict with status information
        """
        success, output = self._run_systemctl("status", service_name)
        
        if not success:
            # Service might not exist or be stopped
            is_active, _ = self._run_systemctl("is-active", service_name)
            return {
                "active": is_active,
                "status": "stopped" if not is_active else "unknown",
                "pid": None
            }
        
        # Parse status output
        is_active, _ = self._run_systemctl("is-active", service_name)
        is_enabled, _ = self._run_systemctl("is-enabled", service_name)
        
        # Try to get PID
        pid = None
        try:
            result = subprocess.run(
                ["systemctl", "show", service_name, "--property=MainPID", "--value"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                pid_str = result.stdout.strip()
                if pid_str and pid_str != "0":
                    pid = int(pid_str)
        except:
            pass
        
        return {
            "active": is_active,
            "enabled": is_enabled,
            "status": "running" if is_active else "stopped",
            "pid": pid,
            "output": output
        }


# Global instance
_systemd_manager: Optional[SystemdServiceManager] = None

def get_systemd_manager() -> SystemdServiceManager:
    """Get global systemd service manager instance."""
    global _systemd_manager
    if _systemd_manager is None:
        _systemd_manager = SystemdServiceManager()
    return _systemd_manager
