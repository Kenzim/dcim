"""
Base plugin interface for server management modules.

All server plugins must inherit from ServerPlugin and implement
the methods for the categories they support.
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from enum import Enum


class PluginCategory(str, Enum):
    """Categories of functionality that plugins can support"""
    POWER_CONTROL = "power_control"
    USER_ACCOUNT_CONTROL = "user_account_control"
    BOOT_ORDER_CONTROL = "boot_order_control"


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
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the plugin with configuration.
        
        Args:
            config: Dictionary containing plugin-specific configuration
                    (e.g., hostname, credentials, connection parameters)
        """
        self.config = config
    
    def get_plugin_info(self) -> Dict[str, Any]:
        """Get plugin metadata"""
        return {
            "name": self.PLUGIN_NAME,
            "version": self.PLUGIN_VERSION,
            "supported_categories": [cat.value for cat in self.SUPPORTED_CATEGORIES],
            "config_template": self.CONFIG_TEMPLATE
        }
    
    def supports_category(self, category: PluginCategory) -> bool:
        """Check if plugin supports a specific category"""
        return category in self.SUPPORTED_CATEGORIES
    
    # ========== Power Control Methods ==========
    
    @abstractmethod
    def get_power_state(self) -> PowerState:
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
    def power_on(self) -> bool:
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
    def power_off(self, force: bool = False) -> bool:
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
    def power_reset(self) -> bool:
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
    def list_users(self) -> List[Dict[str, Any]]:
        """
        List all user accounts on the server.
        
        Returns:
            List of dictionaries with user information (username, roles, etc.)
        
        Raises:
            NotImplementedError: If USER_ACCOUNT_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.USER_ACCOUNT_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support user account control")
    
    @abstractmethod
    def create_user(self, username: str, password: str, roles: Optional[List[str]] = None) -> bool:
        """
        Create a new user account on the server.
        
        Args:
            username: Username for the new account
            password: Password for the new account
            roles: Optional list of roles/permissions
        
        Returns:
            bool: True if user was created successfully, False otherwise
        
        Raises:
            NotImplementedError: If USER_ACCOUNT_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.USER_ACCOUNT_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support user account control")
    
    @abstractmethod
    def delete_user(self, username: str) -> bool:
        """
        Delete a user account from the server.
        
        Args:
            username: Username to delete
        
        Returns:
            bool: True if user was deleted successfully, False otherwise
        
        Raises:
            NotImplementedError: If USER_ACCOUNT_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.USER_ACCOUNT_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support user account control")
    
    @abstractmethod
    def update_user_password(self, username: str, new_password: str) -> bool:
        """
        Update a user's password.
        
        Args:
            username: Username to update
            new_password: New password
        
        Returns:
            bool: True if password was updated successfully, False otherwise
        
        Raises:
            NotImplementedError: If USER_ACCOUNT_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.USER_ACCOUNT_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support user account control")
    
    # ========== Boot Order Control Methods ==========
    
    @abstractmethod
    def get_boot_order(self) -> List[str]:
        """
        Get current boot order.
        
        Returns:
            List of boot device identifiers in order (e.g., ["PXE", "HDD", "CDROM"])
        
        Raises:
            NotImplementedError: If BOOT_ORDER_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.BOOT_ORDER_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support boot order control")
    
    @abstractmethod
    def set_boot_order(self, boot_devices: List[str]) -> bool:
        """
        Set the boot order.
        
        Args:
            boot_devices: List of boot device identifiers in desired order
        
        Returns:
            bool: True if boot order was set successfully, False otherwise
        
        Raises:
            NotImplementedError: If BOOT_ORDER_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.BOOT_ORDER_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support boot order control")
    
    @abstractmethod
    def set_next_boot_device(self, device: str) -> bool:
        """
        Set the device for the next boot only (one-time boot).
        
        Args:
            device: Boot device identifier (e.g., "PXE", "HDD")
        
        Returns:
            bool: True if next boot device was set successfully, False otherwise
        
        Raises:
            NotImplementedError: If BOOT_ORDER_CONTROL not in SUPPORTED_CATEGORIES
        """
        if PluginCategory.BOOT_ORDER_CONTROL not in self.SUPPORTED_CATEGORIES:
            raise NotImplementedError(f"{self.PLUGIN_NAME} does not support boot order control")

