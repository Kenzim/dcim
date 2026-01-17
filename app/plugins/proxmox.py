"""
Proxmox plugin for VM management via Proxmox VE API.

Uses Proxmox REST API for VM power control.
"""
import httpx
import logging
from typing import Dict, Any, Optional
from app.plugins.base import (
    ServerPlugin,
    PluginCategory,
    PowerState
)

logger = logging.getLogger(__name__)


class ProxmoxPlugin(ServerPlugin):
    """
    Proxmox plugin for VM power management.
    
    Uses Proxmox VE REST API for VM power control.
    
    This plugin supports POWER_CONTROL only:
    - test_connection: Test Proxmox API connection and authentication
    - get_power_state: Get current VM power state (on/off/unknown)
    - power_on: Power on the VM
    - power_off: Power off the VM (soft or hard)
    - power_reset: Reset/reboot the VM
    """
    
    PLUGIN_NAME = "proxmox"
    PLUGIN_VERSION = "1.0.0"
    SUPPORTED_CATEGORIES = [
        PluginCategory.POWER_CONTROL,  # Power control only
    ]
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            "hostname": {
                "type": "string",
                "title": "Proxmox Host",
                "description": "Proxmox VE hostname or IP address",
                "required": True
            },
            "username": {
                "type": "string",
                "title": "Username",
                "description": "Proxmox username (e.g., root@pam or user@pve)",
                "required": True
            },
            "password": {
                "type": "string",
                "title": "Password",
                "description": "Proxmox password",
                "format": "password",
                "required": True
            },
            "port": {
                "type": "integer",
                "title": "Port",
                "description": "Proxmox API port (default: 8006)",
                "default": 8006,
                "required": False
            },
            "node": {
                "type": "string",
                "title": "Node Name",
                "description": "Proxmox node name (e.g., proxmox, pve)",
                "required": True
            },
            "vmid": {
                "type": "integer",
                "title": "VM ID",
                "description": "Virtual machine ID",
                "required": True
            },
            "verify_ssl": {
                "type": "boolean",
                "title": "Verify SSL",
                "description": "Verify SSL certificate (default: false for self-signed certs)",
                "default": False,
                "required": False
            }
        },
        "required": ["hostname", "username", "password", "node", "vmid"]
    }
    
    def __init__(self, config: Dict):
        """Initialize plugin with config."""
        super().__init__(config)
        
        self.hostname = config.get("hostname")
        self.username = config.get("username")
        self.password = config.get("password")
        self.port = config.get("port", 8006)
        self.node = config.get("node")
        self.vmid = config.get("vmid")
        self.verify_ssl = config.get("verify_ssl", False)
        
        # Build base URL
        self.base_url = f"https://{self.hostname}:{self.port}"
        
        # Proxmox API requires a ticket (CSRF token) for authentication
        self.ticket = None
        self.csrf_token = None
    
    async def _get_auth_ticket(self) -> Dict[str, str]:
        """
        Authenticate with Proxmox API and get ticket/CSRF token.
        
        Returns:
            Dict with 'ticket' and 'CSRFPreventionToken'
        """
        url = f"{self.base_url}/api2/json/access/ticket"
        
        try:
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=10.0) as client:
                response = await client.post(
                    url,
                    data={
                        "username": self.username,
                        "password": self.password
                    }
                )
                response.raise_for_status()
                
                data = response.json()
                if data.get("data"):
                    ticket_data = data["data"]
                    return {
                        "ticket": ticket_data.get("ticket"),
                        "CSRFPreventionToken": ticket_data.get("CSRFPreventionToken")
                    }
                else:
                    raise Exception("No authentication data returned")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise Exception("Authentication failed: Invalid username or password")
            raise Exception(f"HTTP error: {e.response.status_code}")
        except Exception as e:
            raise Exception(f"Failed to authenticate: {str(e)}")
    
    async def _get_headers(self) -> Dict[str, str]:
        """Get HTTP headers with authentication."""
        if not self.ticket:
            auth_data = await self._get_auth_ticket()
            self.ticket = auth_data["ticket"]
            self.csrf_token = auth_data["CSRFPreventionToken"]
        
        return {
            "Cookie": f"PVEAuthCookie={self.ticket}",
            "CSRFPreventionToken": self.csrf_token
        }
    
    async def test_connection(self) -> Dict[str, Any]:
        """
        Test the Proxmox API connection and verify credentials.
        
        Returns:
            Dict with success status, message, and details
        """
        try:
            logger.info(f"[ProxmoxPlugin.test_connection] Testing Proxmox connection to {self.hostname}:{self.port}")
            
            # Authenticate
            auth_data = await self._get_auth_ticket()
            self.ticket = auth_data["ticket"]
            self.csrf_token = auth_data["CSRFPreventionToken"]
            
            # Test by getting VM status
            url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/status/current"
            headers = await self._get_headers()
            
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=10.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                
                vm_data = response.json().get("data", {})
                power_state = vm_data.get("status", "unknown")
                
                logger.info(f"[ProxmoxPlugin.test_connection] Connection test successful for VM {self.vmid}")
                return {
                    "success": True,
                    "message": f"Successfully connected and authenticated to Proxmox API",
                    "details": {
                        "hostname": self.hostname,
                        "port": self.port,
                        "node": self.node,
                        "vmid": self.vmid,
                        "power_state": power_state,
                        "vm_name": vm_data.get("name", "Unknown"),
                        "cpu": vm_data.get("cpu", 0),
                        "mem": vm_data.get("mem", 0),
                        "maxmem": vm_data.get("maxmem", 0)
                    }
                }
                
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return {
                    "success": False,
                    "message": "Authentication failed: Invalid username or password",
                    "details": {
                        "hostname": self.hostname,
                        "port": self.port,
                        "error": "401 Unauthorized"
                    }
                }
            elif e.response.status_code == 404:
                return {
                    "success": False,
                    "message": f"VM {self.vmid} not found on node {self.node}",
                    "details": {
                        "hostname": self.hostname,
                        "node": self.node,
                        "vmid": self.vmid,
                        "error": "404 Not Found"
                    }
                }
            else:
                return {
                    "success": False,
                    "message": f"HTTP error: {e.response.status_code}",
                    "details": {
                        "hostname": self.hostname,
                        "port": self.port,
                        "error": str(e)
                    }
                }
        except Exception as e:
            error_str = str(e)
            logger.error(f"[ProxmoxPlugin.test_connection] Connection test failed: {error_str}")
            
            if "timeout" in error_str.lower() or "connect" in error_str.lower():
                return {
                    "success": False,
                    "message": f"Connection failed: Could not reach {self.hostname}:{self.port}",
                    "details": {
                        "hostname": self.hostname,
                        "port": self.port,
                        "error": error_str
                    }
                }
            else:
                return {
                    "success": False,
                    "message": f"Connection test failed: {error_str}",
                    "details": {
                        "hostname": self.hostname,
                        "port": self.port,
                        "error": error_str
                    }
                }
    
    async def get_power_state(self) -> PowerState:
        """
        Get current VM power state from Proxmox API.
        
        Returns:
            PowerState enum value (on/off/unknown)
        """
        try:
            url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/status/current"
            headers = await self._get_headers()
            
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=10.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                
                vm_data = response.json().get("data", {})
                status = vm_data.get("status", "unknown").lower()
                
                if status == "running":
                    return PowerState.ON
                elif status == "stopped":
                    return PowerState.OFF
                else:
                    return PowerState.UNKNOWN
                    
        except Exception as e:
            logger.error(f"[ProxmoxPlugin.get_power_state] Failed to get power state: {str(e)}")
            return PowerState.UNKNOWN
    
    async def power_on(self) -> bool:
        """
        Power on the VM via Proxmox API.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/status/start"
            headers = await self._get_headers()
            
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
                response = await client.post(url, headers=headers)
                response.raise_for_status()
                
                logger.info(f"[ProxmoxPlugin.power_on] Successfully sent power on command for VM {self.vmid}")
                return True
                
        except Exception as e:
            logger.error(f"[ProxmoxPlugin.power_on] Failed to power on VM: {str(e)}")
            return False
    
    async def power_off(self, force: bool = False) -> bool:
        """
        Power off the VM via Proxmox API.
        
        Args:
            force: If True, force shutdown (equivalent to pulling power)
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Use shutdown for graceful, stop for force
            action = "stop" if force else "shutdown"
            url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/status/{action}"
            headers = await self._get_headers()
            
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
                response = await client.post(url, headers=headers)
                response.raise_for_status()
                
                logger.info(f"[ProxmoxPlugin.power_off] Successfully sent power off command for VM {self.vmid} (force={force})")
                return True
                
        except Exception as e:
            logger.error(f"[ProxmoxPlugin.power_off] Failed to power off VM: {str(e)}")
            return False
    
    async def power_reset(self) -> bool:
        """
        Reset/reboot the VM via Proxmox API.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/status/reboot"
            headers = await self._get_headers()
            
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
                response = await client.post(url, headers=headers)
                response.raise_for_status()
                
                logger.info(f"[ProxmoxPlugin.power_reset] Successfully sent reset command for VM {self.vmid}")
                return True
                
        except Exception as e:
            logger.error(f"[ProxmoxPlugin.power_reset] Failed to reset VM: {str(e)}")
            return False
    
    # Unsupported capabilities
    async def create_user(self, username: str, password: str, privileges: list) -> bool:
        raise NotImplementedError("Proxmox plugin does not support user account control")
    
    async def delete_user(self, username: str) -> bool:
        raise NotImplementedError("Proxmox plugin does not support user account control")
    
    async def update_user_password(self, username: str, new_password: str) -> bool:
        raise NotImplementedError("Proxmox plugin does not support user account control")
    
    async def list_users(self) -> list:
        raise NotImplementedError("Proxmox plugin does not support user account control")
    
    async def set_boot_order(self, boot_order: list) -> bool:
        raise NotImplementedError("Proxmox plugin does not support boot order control")
    
    async def get_boot_order(self) -> list:
        raise NotImplementedError("Proxmox plugin does not support boot order control")
    
    async def set_next_boot_device(self, device: str) -> bool:
        raise NotImplementedError("Proxmox plugin does not support boot order control")
