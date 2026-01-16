"""
IPMI plugin for server management via IPMI protocol.

Uses ipmitool CLI for IPMI communication.
"""
import logging
import asyncio
import shutil
from typing import Dict, Any, Tuple
from app.plugins.base import (
    ServerPlugin,
    PluginCategory,
    PowerState
)

logger = logging.getLogger(__name__)


class IPMIPlugin(ServerPlugin):
    """
    IPMI plugin for server power management.
    
    Uses ipmitool CLI for IPMI communication.
    
    This plugin supports POWER_CONTROL only:
    - test_connection: Test IPMI connection and authentication
    - get_power_state: Get current power state (on/off/unknown)
    - power_on: Power on the server
    - power_off: Power off the server (soft or hard)
    - power_reset: Reset/reboot the server
    """
    
    PLUGIN_NAME = "ipmi"
    PLUGIN_VERSION = "1.0.0"
    SUPPORTED_CATEGORIES = [
        PluginCategory.POWER_CONTROL,  # Power control only
    ]
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            "hostname": {
                "type": "string",
                "title": "Hostname/IP",
                "description": "IPMI BMC hostname or IP address",
                "required": True
            },
            "username": {
                "type": "string",
                "title": "Username",
                "description": "IPMI BMC username",
                "required": True
            },
            "password": {
                "type": "string",
                "title": "Password",
                "description": "IPMI BMC password",
                "format": "password",
                "required": True
            },
            "port": {
                "type": "integer",
                "title": "Port",
                "description": "IPMI port (default: 623)",
                "default": 623,
                "required": False
            },
            "timeout": {
                "type": "integer",
                "title": "Timeout (seconds)",
                "description": "Command timeout in seconds (default: 30)",
                "default": 30,
                "required": False
            }
        },
        "required": ["hostname", "username", "password"]
    }
    
    def __init__(self, config: Dict):
        """Initialize plugin with config."""
        super().__init__(config)
        
        # Check if ipmitool is available
        if not shutil.which("ipmitool"):
            raise ImportError(
                "ipmitool is not installed. Install it with: apt-get install ipmitool (Debian/Ubuntu) "
                "or yum install ipmitool (RHEL/CentOS)"
            )
    
    def _build_ipmitool_args(self, command: str) -> list:
        """
        Build ipmitool command arguments.
        
        Args:
            command: ipmitool subcommand (e.g., "power status", "mc info")
            
        Returns:
            List of command arguments
        """
        config = self.config
        hostname = config.get("hostname")
        username = config.get("username")
        password = config.get("password")
        port = config.get("port", 623)
        
        args = [
            "ipmitool",
            "-H", str(hostname),
            "-U", str(username),
            "-P", str(password),
            "-p", str(port),
            "-I", "lanplus",  # Use LAN+ interface (more reliable)
        ]
        
        # Add the command
        args.extend(command.split())
        
        return args
    
    async def _run_ipmitool(self, command: str, timeout: int = 30) -> Tuple[str, str, int]:
        """
        Run an ipmitool command asynchronously.
        
        Args:
            command: ipmitool subcommand
            timeout: Command timeout in seconds
            
        Returns:
            Tuple of (stdout, stderr, returncode)
            
        Raises:
            Exception: If command fails or times out
        """
        args = self._build_ipmitool_args(command)
        
        logger.debug(f"[IPMIPlugin] Running: {' '.join(args[:6])} ... {command}")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=float(timeout)
            )
            
            stdout_str = stdout.decode('utf-8', errors='ignore').strip()
            stderr_str = stderr.decode('utf-8', errors='ignore').strip()
            returncode = process.returncode
            
            if returncode != 0:
                error_msg = stderr_str or stdout_str or f"ipmitool exited with code {returncode}"
                logger.error(f"[IPMIPlugin] Command failed: {error_msg}")
                raise Exception(f"ipmitool command failed: {error_msg}")
            
            return stdout_str, stderr_str, returncode
            
        except asyncio.TimeoutError:
            logger.error(f"[IPMIPlugin] Command timed out after {timeout} seconds")
            raise Exception(f"ipmitool command timed out after {timeout} seconds")
        except Exception as e:
            logger.error(f"[IPMIPlugin] Command error: {str(e)}")
            raise
    
    async def test_connection(self) -> Dict[str, Any]:
        """
        Test the IPMI connection and verify credentials.
        
        Returns:
            Dict with success status, message, and details
        """
        config = self.config
        hostname = config.get("hostname")
        port = config.get("port", 623)
        timeout = config.get("timeout", 30)
        
        try:
            logger.info(f"[IPMIPlugin.test_connection] Testing IPMI connection to {hostname}:{port}")
            
            # Test connection by getting MC info
            stdout, _, _ = await self._run_ipmitool("mc info", timeout=timeout)
            
            # Parse device info from mc info output
            device_info = {}
            power_state = 'unknown'
            
            # Try to get power state
            try:
                power_stdout, _, _ = await self._run_ipmitool("power status", timeout=timeout)
                if "on" in power_stdout.lower():
                    power_state = 'on'
                elif "off" in power_stdout.lower():
                    power_state = 'off'
            except Exception as e:
                logger.warning(f"[IPMIPlugin.test_connection] Could not get power state: {str(e)}")
            
            # Extract device info from mc info
            for line in stdout.split('\n'):
                line = line.strip()
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip().lower().replace(' ', '_')
                    value = value.strip()
                    if value:
                        device_info[key] = value
            
            logger.info(f"[IPMIPlugin.test_connection] Connection test successful for {hostname}")
            return {
                "success": True,
                "message": f"Successfully connected and authenticated to IPMI BMC",
                "details": {
                    "hostname": hostname,
                    "port": port,
                    "power_state": power_state,
                    **device_info
                }
            }
            
        except Exception as e:
            error_str = str(e)
            logger.error(f"[IPMIPlugin.test_connection] Connection test failed: {error_str}")
            
            # Provide helpful error messages
            if "authentication" in error_str.lower() or "invalid" in error_str.lower() or "unauthorized" in error_str.lower():
                return {
                    "success": False,
                    "message": "Authentication failed: Invalid username or password",
                    "details": {
                        "hostname": hostname,
                        "port": port,
                        "error": error_str
                    }
                }
            elif "timeout" in error_str.lower():
                return {
                    "success": False,
                    "message": f"Connection timeout: Could not reach {hostname}:{port} within {timeout} seconds",
                    "details": {
                        "hostname": hostname,
                        "port": port,
                        "timeout": timeout,
                        "error": error_str
                    }
                }
            elif "connection" in error_str.lower() or "refused" in error_str.lower():
                return {
                    "success": False,
                    "message": f"Connection failed: Could not reach {hostname}:{port}. Check network connectivity and firewall settings.",
                    "details": {
                        "hostname": hostname,
                        "port": port,
                        "error": error_str
                    }
                }
            else:
                return {
                    "success": False,
                    "message": f"Connection test failed: {error_str}",
                    "details": {
                        "hostname": hostname,
                        "port": port,
                        "error": error_str
                    }
                }
    
    # ========== Power Control Methods ==========
    
    async def get_power_state(self) -> PowerState:
        """Get current power state via IPMI."""
        try:
            stdout, _, _ = await self._run_ipmitool("power status")
            
            stdout_lower = stdout.lower()
            if "on" in stdout_lower:
                return PowerState.ON
            elif "off" in stdout_lower:
                return PowerState.OFF
            else:
                logger.warning(f"[IPMIPlugin.get_power_state] Unknown power state: {stdout}")
                return PowerState.UNKNOWN
                
        except Exception as e:
            logger.error(f"[IPMIPlugin.get_power_state] Failed to get power state: {str(e)}")
            raise Exception(f"Failed to get power state: {str(e)}")
    
    async def power_on(self) -> bool:
        """Power on the server via IPMI."""
        try:
            await self._run_ipmitool("power on")
            logger.info(f"[IPMIPlugin.power_on] Successfully powered on server {self.config.get('hostname')}")
            return True
        except Exception as e:
            logger.error(f"[IPMIPlugin.power_on] Failed to power on server: {str(e)}")
            raise Exception(f"Failed to power on server: {str(e)}")
    
    async def power_off(self, force: bool = False) -> bool:
        """
        Power off the server via IPMI.
        
        Args:
            force: If True, force power off (hard shutdown)
        """
        try:
            if force:
                await self._run_ipmitool("power off -f")
            else:
                await self._run_ipmitool("power soft")
            logger.info(f"[IPMIPlugin.power_off] Successfully powered off server {self.config.get('hostname')} (force={force})")
            return True
        except Exception as e:
            logger.error(f"[IPMIPlugin.power_off] Failed to power off server: {str(e)}")
            raise Exception(f"Failed to power off server: {str(e)}")
    
    async def power_reset(self) -> bool:
        """Reset/reboot the server via IPMI."""
        try:
            await self._run_ipmitool("power reset")
            logger.info(f"[IPMIPlugin.power_reset] Successfully reset server {self.config.get('hostname')}")
            return True
        except Exception as e:
            logger.error(f"[IPMIPlugin.power_reset] Failed to reset server: {str(e)}")
            raise Exception(f"Failed to reset server: {str(e)}")
    
    # ========== User Account Control Methods ==========
    # Not supported by this plugin
    
    async def list_users(self):
        raise NotImplementedError("IPMI plugin does not support user account control")
    
    async def create_user(self, username: str, password: str, roles=None):
        raise NotImplementedError("IPMI plugin does not support user account control")
    
    async def delete_user(self, username: str):
        raise NotImplementedError("IPMI plugin does not support user account control")
    
    async def update_user_password(self, username: str, new_password: str):
        raise NotImplementedError("IPMI plugin does not support user account control")
    
    # ========== Boot Order Control Methods ==========
    # Not supported by this plugin
    
    async def get_boot_order(self):
        raise NotImplementedError("IPMI plugin does not support boot order control")
    
    async def set_boot_order(self, boot_devices):
        raise NotImplementedError("IPMI plugin does not support boot order control")
    
    async def set_next_boot_device(self, device: str):
        raise NotImplementedError("IPMI plugin does not support boot order control")
