"""
Hardcoded plugin capabilities mapping.

This module defines the capabilities that each plugin supports.
Capabilities are hardcoded here rather than stored in the database.
"""

# Server plugin capabilities
SERVER_CAPABILITIES = {
    "power_control": {
        "name": "power_control",
        "display_name": "Power Control",
        "description": "Control server power state (on/off/reset)"
    },
    "user_control": {
        "name": "user_control",
        "display_name": "User Control",
        "description": "Manage user accounts on the server"
    }
}

# Switch plugin capabilities
SWITCH_CAPABILITIES = {
    "monitoring": {
        "name": "monitoring",
        "display_name": "Monitoring",
        "description": "Monitor switch ports and statistics"
    },
    "port_control": {
        "name": "port_control",
        "display_name": "Port Control",
        "description": "Control switch ports (enable/disable)"
    }
}

# Mapping of plugin names to their supported capabilities
SERVER_PLUGIN_CAPABILITIES = {
    "proxmox": ["power_control"],
    "ipmi": ["power_control"],
}

SWITCH_PLUGIN_CAPABILITIES = {
    "snmpv3": ["monitoring"],
}


def get_server_plugin_capabilities(plugin_name: str) -> list[str]:
    """Get list of capabilities for a server plugin by name."""
    return SERVER_PLUGIN_CAPABILITIES.get(plugin_name, [])


def get_switch_plugin_capabilities(plugin_name: str) -> list[str]:
    """Get list of capabilities for a switch plugin by name."""
    return SWITCH_PLUGIN_CAPABILITIES.get(plugin_name, [])


def server_plugin_supports(plugin_name: str, capability: str) -> bool:
    """Check if a server plugin supports a specific capability."""
    return capability in get_server_plugin_capabilities(plugin_name)


def switch_plugin_supports(plugin_name: str, capability: str) -> bool:
    """Check if a switch plugin supports a specific capability."""
    return capability in get_switch_plugin_capabilities(plugin_name)
