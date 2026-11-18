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
    LLDP_DISCOVERY = "lldp_discovery"  # Read LLDP neighbour tables
    VLAN_MANAGEMENT = "vlan_management"  # Create/delete VLANs, change the VLANs a port carries


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
    # True when the plugin honours ``dry_run``: configuration batches are
    # recorded in ``planned_batches`` instead of being sent, while read-only
    # queries still go to the switch.
    SUPPORTS_DRY_RUN: bool = False
    # True when the plugin implements the vPC methods below.
    SUPPORTS_VPC: bool = False
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the plugin with configuration.
        
        Args:
            config: Dictionary containing plugin-specific configuration
                    (e.g., hostname, SNMP community, credentials, etc.)
        """
        self.config = config
        self.dry_run = False
        self.planned_batches: List[List[str]] = []
    
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
    
    async def delete_vlan(self, vlan_id: int, name: Optional[str] = None) -> Dict[str, Any]:
        """Delete a VLAN. With ``name``, a plugin that can read VLAN names leaves a VLAN of another name alone."""
        raise NotImplementedError("VLAN management not supported by this plugin")

    async def describe_port(self, port: str) -> Dict[str, Any]:
        """Where a port's VLAN settings live, before anything is changed.

        Returns 'interface' (the port, or its port-channel when bundled),
        'port_channel' (name or None), 'vpc_id' (int or None), 'members'
        (member ports of the port-channel, or the port itself) and
        'peer_link' (True for a vPC peer-link or one of its members).
        """
        return {"interface": port, "port_channel": None, "vpc_id": None, "members": [port], "peer_link": False}

    # Optional vPC methods (SUPPORTS_VPC)

    async def find_vpc_interface(self, vpc_id: int) -> Optional[str]:
        """The port-channel configured with ``vpc <vpc_id>``, or None"""
        raise NotImplementedError("vPC not supported by this plugin")

    async def add_peer_link_vlan(self, vlan_id: int) -> Dict[str, Any]:
        """Make the vPC peer-link carry ``vlan_id``; a no-op when it carries every VLAN"""
        raise NotImplementedError("vPC not supported by this plugin")

    async def remove_peer_link_vlan(self, vlan_id: int) -> Dict[str, Any]:
        """Stop the vPC peer-link carrying ``vlan_id`` when its allowed list is explicit"""
        raise NotImplementedError("vPC not supported by this plugin")

    async def check_vpc_vlan(self, vpc_id: int, vlan_id: int, expect_up: bool = True) -> List[str]:
        """Problems with a vPC after a change (consistency, VLAN state); empty when healthy"""
        raise NotImplementedError("vPC not supported by this plugin")

    async def get_port_vlans(self, port: str) -> Dict[str, Any]:
        """
        Get the VLANs a port carries.

        Returns:
            Dict with 'interface' (the interface the settings live on, which is
            the port-channel for a bundled port), 'mode' ("access" or "trunk"),
            'native_vlan' (int) and 'allowed_vlans' (list of int)
        """
        raise NotImplementedError("VLAN management not supported by this plugin")

    async def add_port_vlan(self, port: str, vlan_id: int) -> Dict[str, Any]:
        """Carry ``vlan_id`` tagged on a port, keeping its untagged VLAN as it is"""
        raise NotImplementedError("VLAN management not supported by this plugin")

    async def remove_port_vlan(self, port: str, vlan_id: int) -> Dict[str, Any]:
        """Stop carrying ``vlan_id`` tagged on a port"""
        raise NotImplementedError("VLAN management not supported by this plugin")
    
    # Optional methods for LLDP_DISCOVERY category
    
    async def get_lldp_neighbors(self) -> List[Dict[str, Any]]:
        """Get LLDP neighbour information.

        Returns one dict per neighbour with: local_port_num, local_port_id, local_port_desc,
        chassis_id_subtype, chassis_id, port_id_subtype, port_id, port_desc, sys_name, sys_desc.
        """
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
