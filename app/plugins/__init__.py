"""
Server management plugin system.

Plugins provide a common interface for managing different types of servers
with varying capabilities (IPMI, Redfish, SSH, vendor APIs, etc.).
"""
from app.plugins.base import (
    ServerPlugin,
    PluginCategory,
    PowerState
)
from app.plugins.registry import (
    PluginRegistry,
    get_registry
)

__all__ = [
    "ServerPlugin",
    "PluginCategory",
    "PowerState",
    "PluginRegistry",
    "get_registry",
]



