"""
Base plugin interface for server management modules.

All server plugins must inherit from ServerPlugin and implement
the methods for the categories they support.

Plugins define CAPABILITIES (list of Capability) with UI schema
for config-driven frontend rendering. Optional capabilities are
enabled per-server via plugin_config["enabled_capabilities"].
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, TYPE_CHECKING
from enum import Enum

if TYPE_CHECKING:
    from app.plugins.capabilities import Capability


class PluginCategory(str, Enum):
    """Categories of functionality that server plugins can support"""
    POWER_CONTROL = "power_control"
    USER_CONTROL = "user_control"


class PowerState(str, Enum):
    """Server power states"""
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


class ServerPlugin(ABC):
    """
    Base class for all server management plugins.
    
    Each plugin must:
    1. Set PLUGIN_NAME and PLUGIN_VERSION class attributes
    2. Set SUPPORTED_CATEGORIES list of PluginCategory values
    3. Set CONFIG_TEMPLATE as a JSON schema defining required configuration fields
    4. Implement methods for each supported category
    """
    
    # Plugin metadata - must be set by subclasses
    PLUGIN_NAME: str = ""
    PLUGIN_VERSION: str = ""
    SUPPORTED_CATEGORIES: List[PluginCategory] = []
    CONFIG_TEMPLATE: Dict[str, Any] = {}  # JSON schema for configuration form
    CAPABILITIES: List["Capability"] = []  # Capability definitions with UI schema
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the plugin with configuration.
        
        Args:
            config: Dictionary containing plugin-specific configuration
                    (e.g., hostname, credentials, connection parameters)
        """
        self.config = config
    
    def get_plugin_info(self) -> Dict[str, Any]:
        """Get plugin metadata including capabilities with UI schema."""
        result = {
            "name": self.PLUGIN_NAME,
            "version": self.PLUGIN_VERSION,
            "supported_categories": [cat.value for cat in self.SUPPORTED_CATEGORIES],
            "config_template": self.CONFIG_TEMPLATE,
        }
        if self.CAPABILITIES:
            result["capabilities"] = [c.to_dict() for c in self.CAPABILITIES]
        return result
    
    def get_capabilities(self) -> List[str]:
        """
        Get list of capabilities this plugin claims to support.
        
        Returns:
            List of capability names (e.g., ["test_connection", "get_power_state", "power_on", "power_off", "power_reset"])
        """
        capabilities = ["test_connection"]
        
        if PluginCategory.POWER_CONTROL in self.SUPPORTED_CATEGORIES:
            capabilities.extend([
                "get_power_state",
                "power_on",
                "power_off",
                "power_reset"
            ])
        
        if PluginCategory.USER_CONTROL in self.SUPPORTED_CATEGORIES:
            capabilities.extend([
                "list_users",
                "create_user",
                "delete_user",
                "update_user_password"
            ])
        
        return capabilities
    
    def supports_category(self, category: PluginCategory) -> bool:
        """Check if plugin supports a specific category"""
        return category in self.SUPPORTED_CATEGORIES
    
    @abstractmethod
    async def test_connection(self) -> Dict[str, Any]:
        """
        Test the plugin connection and verify credentials.
        
        This method should attempt to connect to the server management interface
        and verify that the provided credentials are valid.
        
        Returns:
            Dict with keys:
                - success: bool - True if connection test passed
                - message: str - Human-readable message about the test result
                - details: Optional[Dict] - Additional details (e.g., server info, version, etc.)
        
        Raises:
            Exception: If connection test fails with a specific error
        """
        pass
    
    # ========== Power Control Methods ==========
    
    @abstractmethod
    async def get_power_state(self) -> PowerState:
        """
        Get current power state of the server.
        
        Returns:
            PowerState: Current power state (ON, OFF, or UNKNOWN)
        
        Raises:
            NotImplementedError: If POWER_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.POWER_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support power control")
    
    @abstractmethod
    async def power_on(self) -> bool:
        """
        Power on the server.
        
        Returns:
            bool: True if command was successful, False otherwise
        
        Raises:
            NotImplementedError: If POWER_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.POWER_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support power control")
    
    @abstractmethod
    async def power_off(self, force: bool = False) -> bool:
        """
        Power off the server.
        
        Args:
            force: If True, force power off (e.g., hard shutdown)
        
        Returns:
            bool: True if command was successful, False otherwise
        
        Raises:
            NotImplementedError: If POWER_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.POWER_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support power control")
    
    @abstractmethod
    async def power_reset(self) -> bool:
        """
        Reset/reboot the server.
        
        Returns:
            bool: True if command was successful, False otherwise
        
        Raises:
            NotImplementedError: If POWER_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.POWER_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support power control")
    
    # ========== User Account Control Methods ==========
    
    @abstractmethod
    async def list_users(self) -> List[Dict[str, Any]]:
        """
        List all user accounts on the server.
        
        Returns:
            List of dictionaries with user information (username, roles, etc.)
        
        Raises:
            NotImplementedError: If USER_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.USER_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support user control")
    
    @abstractmethod
    async def create_user(self, username: str, password: str, roles: Optional[List[str]] = None) -> bool:
        """
        Create a new user account on the server.
        
        Args:
            username: Username for the new account
            password: Password for the new account
            roles: Optional list of roles/permissions
        
        Returns:
            bool: True if user was created successfully, False otherwise
        
        Raises:
            NotImplementedError: If USER_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.USER_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support user control")
    
    @abstractmethod
    async def delete_user(self, username: str) -> bool:
        """
        Delete a user account from the server.
        
        Args:
            username: Username to delete
        
        Returns:
            bool: True if user was deleted successfully, False otherwise
        
        Raises:
            NotImplementedError: If USER_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.USER_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support user control")
    
    @abstractmethod
    async def update_user_password(self, username: str, new_password: str) -> bool:
        """
        Update a user's password.
        
        Args:
            username: Username to update
            new_password: New password
        
        Returns:
            bool: True if password was updated successfully, False otherwise
        
        Raises:
            NotImplementedError: If USER_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.USER_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support user control")

