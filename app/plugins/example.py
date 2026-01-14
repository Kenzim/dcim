"""
Example plugin demonstrating the plugin interface.

This is a stub/example plugin that shows how to implement
a server management plugin. Real plugins would connect to
actual server management interfaces (IPMI, Redfish, etc.).
"""
from typing import Dict, List, Optional, Any
from app.plugins.base import (
    ServerPlugin,
    PluginCategory,
    PowerState
)


class ExamplePlugin(ServerPlugin):
    """
    Example plugin that supports all three categories.
    
    This is a demonstration plugin - real plugins would
    connect to actual server management interfaces.
    """
    
    PLUGIN_NAME = "example"
    PLUGIN_VERSION = "1.0.0"
    SUPPORTED_CATEGORIES = [
        PluginCategory.POWER_CONTROL,
        PluginCategory.USER_ACCOUNT_CONTROL,
        PluginCategory.BOOT_ORDER_CONTROL,
    ]
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            "hostname": {
                "type": "string",
                "title": "Hostname",
                "description": "Server hostname or IP address",
                "required": True
            },
            "username": {
                "type": "string",
                "title": "Username",
                "description": "Username for authentication",
                "required": True
            },
            "password": {
                "type": "string",
                "title": "Password",
                "description": "Password for authentication",
                "format": "password",
                "required": True
            },
            "port": {
                "type": "integer",
                "title": "Port",
                "description": "Connection port (optional)",
                "default": 22,
                "required": False
            }
        },
        "required": ["hostname", "username", "password"]
    }
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        # Example: store connection info from config
        self.hostname = config.get("hostname", "localhost")
        self.username = config.get("username")
        self.password = config.get("password")
        
        # Example: mock state storage
        self._power_state = PowerState.OFF
        self._users = {}
        self._boot_order = ["HDD", "CDROM", "PXE"]
    
    # ========== Power Control Methods ==========
    
    def get_power_state(self) -> PowerState:
        """Get current power state"""
        return self._power_state
    
    def power_on(self) -> bool:
        """Power on the server"""
        self._power_state = PowerState.ON
        return True
    
    def power_off(self, force: bool = False) -> bool:
        """Power off the server"""
        self._power_state = PowerState.OFF
        return True
    
    def power_reset(self) -> bool:
        """Reset/reboot the server"""
        # In real implementation, would power cycle
        self._power_state = PowerState.OFF
        return True
    
    # ========== User Account Control Methods ==========
    
    def list_users(self) -> List[Dict[str, Any]]:
        """List all users"""
        return [
            {"username": username, "roles": info.get("roles", [])}
            for username, info in self._users.items()
        ]
    
    def create_user(self, username: str, password: str, roles: Optional[List[str]] = None) -> bool:
        """Create a new user"""
        if username in self._users:
            return False
        
        self._users[username] = {
            "password": password,  # In real implementation, would hash this
            "roles": roles or []
        }
        return True
    
    def delete_user(self, username: str) -> bool:
        """Delete a user"""
        if username not in self._users:
            return False
        
        del self._users[username]
        return True
    
    def update_user_password(self, username: str, new_password: str) -> bool:
        """Update user password"""
        if username not in self._users:
            return False
        
        self._users[username]["password"] = new_password
        return True
    
    # ========== Boot Order Control Methods ==========
    
    def get_boot_order(self) -> List[str]:
        """Get current boot order"""
        return self._boot_order.copy()
    
    def set_boot_order(self, boot_devices: List[str]) -> bool:
        """Set boot order"""
        self._boot_order = boot_devices.copy()
        return True
    
    def set_next_boot_device(self, device: str) -> bool:
        """Set next boot device (one-time boot)"""
        # In real implementation, would set one-time boot
        # For example, temporarily move device to front of boot order
        if device in self._boot_order:
            self._boot_order.remove(device)
        self._boot_order.insert(0, device)
        return True

