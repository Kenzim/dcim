"""
Base classes for network switch management plugins.

Similar to server plugins, but specifically for network switches.
Supports protocols like SNMP v2/v3, vendor APIs, etc.
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from enum import Enum


class SwitchPluginCategory(str, Enum):
    """Categories of capabilities that switch plugins can support"""
    MONITORING = "monitoring"  # Port statistics, bandwidth monitoring
    PORT_CONTROL = "port_control"  # Enable/disable ports, configure port settings


class SwitchPlugin(ABC):
    """
    Base class for all network switch management plugins.
    
    Each plugin must:
    1. Set PLUGIN_NAME and PLUGIN_VERSION class attributes
    2. Set SUPPORTED_CATEGORIES list of SwitchPluginCategory values
    3. Set CONFIG_TEMPLATE as a JSON schema defining required configuration fields
    4. Implement methods for each supported category
    """
    
    # Plugin metadata - must be set by subclasses
    PLUGIN_NAME: str = ""
    PLUGIN_VERSION: str = ""
    SUPPORTED_CATEGORIES: List[SwitchPluginCategory] = []
    CONFIG_TEMPLATE: Dict[str, Any] = {}  # JSON schema for configuration form
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the plugin with configuration.
        
        Args:
            config: Dictionary containing plugin-specific configuration
                    (e.g., hostname, SNMP community, credentials, etc.)
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
    
    def get_capabilities(self) -> List[str]:
        """
        Get list of capabilities this plugin supports.
        
        Returns:
            List of capability strings (e.g., ['port_control', 'vlan_management'])
        """
        return [cat.value for cat in self.SUPPORTED_CATEGORIES]
    
    # Required methods that all plugins must implement
    
    @abstractmethod
    async def test_connection(self) -> Dict[str, Any]:
        """
        Test connection to the switch.
        
        Returns:
            Dict with 'success' (bool) and optional 'message' (str) and 'details' (dict)
        """
        pass
    
    # Optional methods for PORT_CONTROL category
    
    async def get_port_status(self, port: str) -> Dict[str, Any]:
        """
        Get status of a specific port.
        
        Args:
            port: Port identifier (e.g., "1", "GigabitEthernet0/1", etc.)
        
        Returns:
            Dict with port status information
        """
        raise NotImplementedError("Port control not supported by this plugin")
    
    async def enable_port(self, port: str) -> Dict[str, Any]:
        """Enable a port"""
        raise NotImplementedError("Port control not supported by this plugin")
    
    async def disable_port(self, port: str) -> Dict[str, Any]:
        """Disable a port"""
        raise NotImplementedError("Port control not supported by this plugin")
    
    # Optional methods for VLAN_MANAGEMENT category
    
    async def list_vlans(self) -> List[Dict[str, Any]]:
        """List all VLANs on the switch"""
        raise NotImplementedError("VLAN management not supported by this plugin")
    
    async def create_vlan(self, vlan_id: int, name: str) -> Dict[str, Any]:
        """Create a new VLAN"""
        raise NotImplementedError("VLAN management not supported by this plugin")
    
    async def delete_vlan(self, vlan_id: int) -> Dict[str, Any]:
        """Delete a VLAN"""
        raise NotImplementedError("VLAN management not supported by this plugin")
    
    # Optional methods for LLDP_DISCOVERY category
    
    async def get_lldp_neighbors(self) -> List[Dict[str, Any]]:
        """Get LLDP neighbor information"""
        raise NotImplementedError("LLDP discovery not supported by this plugin")
    
    # Optional methods for MONITORING category
    
    async def get_port_statistics(self, port: str) -> Dict[str, Any]:
        """Get statistics for a specific port"""
        raise NotImplementedError("Monitoring not supported by this plugin")
    
    async def get_all_port_statistics(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all ports"""
        raise NotImplementedError("Monitoring not supported by this plugin")
    
    # Method to get switch information (model, serial, firmware, port count)
    async def get_switch_info(self) -> Dict[str, Any]:
        """
        Get switch information including model, serial number, firmware version, and port count.
        
        Returns:
            Dict with keys:
                - model: Switch model/manufacturer
                - serial_number: Serial number
                - firmware_version: Firmware/OS version
                - port_count: Number of ports
        """
        raise NotImplementedError("Switch info retrieval not supported by this plugin")
