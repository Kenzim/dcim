"""
Plugin registry for discovering and loading network switch management plugins.
"""
import importlib
import importlib.util
import inspect
import logging
from pathlib import Path
from typing import Dict, List, Optional, Type, Any
from app.plugins.switch_base import SwitchPlugin, SwitchPluginCategory

logger = logging.getLogger(__name__)


class SwitchPluginRegistry:
    """Registry for managing network switch management plugins"""
    
    def __init__(self):
        self._plugins: Dict[str, Type[SwitchPlugin]] = {}
        self._plugin_instances: Dict[str, SwitchPlugin] = {}
    
    def discover_plugins(self, plugin_dir: Optional[Path] = None) -> None:
        """
        Discover and register all switch plugins in the plugins directory.
        
        Args:
            plugin_dir: Path to plugins directory. If None, uses app/plugins/
        """
        if plugin_dir is None:
            # Get the plugins directory relative to this file
            plugin_dir = Path(__file__).parent
        
        logger.info(f"Discovering switch plugins in {plugin_dir}")
        
        # Look for Python files that might contain switch plugins
        # Switch plugins should be in files like snmp2.py, snmp3.py, etc.
        plugin_files = [
            f for f in plugin_dir.glob("*.py")
            if f.stem not in ["__init__", "base", "registry", "switch_base", "switch_registry", "proxmox"]
        ]
        
        for plugin_file in plugin_files:
            try:
                self._load_plugin_file(plugin_file)
            except Exception as e:
                logger.debug(f"File {plugin_file} does not contain switch plugins: {e}")
    
    def _load_plugin_file(self, plugin_file: Path) -> None:
        """Load a switch plugin from a Python file"""
        module_name = f"app.plugins.{plugin_file.stem}"
        
        try:
            spec = importlib.util.spec_from_file_location(module_name, plugin_file)
            if spec is None or spec.loader is None:
                logger.warning(f"Could not create spec for {plugin_file}")
                return
            
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            # Find all classes that inherit from SwitchPlugin
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if (obj != SwitchPlugin and 
                    issubclass(obj, SwitchPlugin) and 
                    obj.PLUGIN_NAME):
                    self.register_plugin(obj)
                    logger.info(f"Registered switch plugin: {obj.PLUGIN_NAME} v{obj.PLUGIN_VERSION}")
        
        except Exception as e:
            logger.debug(f"Error loading plugin file {plugin_file} (may not be a switch plugin): {e}")
    
    def register_plugin(self, plugin_class: Type[SwitchPlugin]) -> None:
        """
        Register a switch plugin class.
        
        Args:
            plugin_class: Plugin class that inherits from SwitchPlugin
        """
        if not plugin_class.PLUGIN_NAME:
            raise ValueError("Plugin must have PLUGIN_NAME set")
        
        plugin_name = plugin_class.PLUGIN_NAME
        if plugin_name in self._plugins:
            logger.warning(f"Switch plugin {plugin_name} already registered, overwriting")
        
        self._plugins[plugin_name] = plugin_class
        logger.debug(f"Registered switch plugin: {plugin_name}")
    
    def get_plugin(self, plugin_name: str, config: Dict[str, Any]) -> SwitchPlugin:
        """
        Get an instance of a plugin with the given configuration.
        
        Args:
            plugin_name: Name of the plugin
            config: Plugin configuration dictionary
        
        Returns:
            Plugin instance
        
        Raises:
            KeyError: If plugin is not registered
        """
        if plugin_name not in self._plugins:
            raise KeyError(f"Switch plugin '{plugin_name}' not found in registry")
        
        # Create a unique key for this plugin instance
        instance_key = f"{plugin_name}:{id(config)}"
        
        # Return existing instance if available, otherwise create new one
        if instance_key not in self._plugin_instances:
            plugin_class = self._plugins[plugin_name]
            self._plugin_instances[instance_key] = plugin_class(config)
        
        return self._plugin_instances[instance_key]
    
    def list_plugins(self) -> List[Dict[str, Any]]:
        """List all registered plugins with their metadata"""
        plugins = []
        for plugin_class in self._plugins.values():
            plugins.append({
                "name": plugin_class.PLUGIN_NAME,
                "version": plugin_class.PLUGIN_VERSION,
                "supported_categories": [cat.value for cat in plugin_class.SUPPORTED_CATEGORIES],
                "config_template": plugin_class.CONFIG_TEMPLATE
            })
        return plugins
    
    def get_plugin_info(self, plugin_name: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a specific plugin"""
        if plugin_name not in self._plugins:
            return None
        
        plugin_class = self._plugins[plugin_name]
        return {
            "name": plugin_class.PLUGIN_NAME,
            "version": plugin_class.PLUGIN_VERSION,
            "supported_categories": [cat.value for cat in plugin_class.SUPPORTED_CATEGORIES],
            "config_template": plugin_class.CONFIG_TEMPLATE
        }

    def supports_monitoring(self, plugin_name: str) -> bool:
        """Return True if the plugin supports MONITORING (e.g. get_all_port_statistics)."""
        if plugin_name not in self._plugins:
            return False
        return SwitchPluginCategory.MONITORING in self._plugins[plugin_name].SUPPORTED_CATEGORIES


# Global registry instance
_switch_registry: Optional[SwitchPluginRegistry] = None


def get_switch_registry() -> SwitchPluginRegistry:
    """Get the global switch plugin registry instance"""
    global _switch_registry
    if _switch_registry is None:
        _switch_registry = SwitchPluginRegistry()
        # Auto-discover plugins on first access
        _switch_registry.discover_plugins()
    return _switch_registry
