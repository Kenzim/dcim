"""
Plugin registry for discovering and loading server management plugins.
"""
import importlib
import importlib.util
import inspect
import logging
from pathlib import Path
from typing import Dict, List, Optional, Type, Any
from app.plugins.base import ServerPlugin, PluginCategory

logger = logging.getLogger(__name__)


class PluginRegistry:
    """Registry for managing server management plugins"""
    
    def __init__(self):
        self._plugins: Dict[str, Type[ServerPlugin]] = {}
        self._plugin_instances: Dict[str, ServerPlugin] = {}
    
    def discover_plugins(self, plugin_dir: Optional[Path] = None) -> None:
        """
        Discover and register all plugins in the plugins directory.
        
        Args:
            plugin_dir: Path to plugins directory. If None, uses app/plugins/
        """
        if plugin_dir is None:
            # Get the plugins directory relative to this file
            plugin_dir = Path(__file__).parent
        
        logger.info(f"Discovering plugins in {plugin_dir}")
        
        # Look for Python files (excluding __init__.py and base.py)
        plugin_files = [
            f for f in plugin_dir.glob("*.py")
            if f.stem not in ["__init__", "base", "registry"]
        ]
        
        for plugin_file in plugin_files:
            try:
                self._load_plugin_file(plugin_file)
            except Exception as e:
                logger.error(f"Failed to load plugin from {plugin_file}: {e}", exc_info=True)
    
    def _load_plugin_file(self, plugin_file: Path) -> None:
        """Load a plugin from a Python file"""
        module_name = f"app.plugins.{plugin_file.stem}"
        
        try:
            spec = importlib.util.spec_from_file_location(module_name, plugin_file)
            if spec is None or spec.loader is None:
                logger.warning(f"Could not create spec for {plugin_file}")
                return
            
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            # Find all classes that inherit from ServerPlugin
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if (obj != ServerPlugin and 
                    issubclass(obj, ServerPlugin) and 
                    obj.PLUGIN_NAME):
                    self.register_plugin(obj)
                    logger.info(f"Registered plugin: {obj.PLUGIN_NAME} v{obj.PLUGIN_VERSION}")
        
        except Exception as e:
            logger.error(f"Error loading plugin file {plugin_file}: {e}", exc_info=True)
    
    def register_plugin(self, plugin_class: Type[ServerPlugin]) -> None:
        """
        Register a plugin class.
        
        Args:
            plugin_class: Plugin class that inherits from ServerPlugin
        """
        if not plugin_class.PLUGIN_NAME:
            raise ValueError("Plugin must have PLUGIN_NAME set")
        
        plugin_name = plugin_class.PLUGIN_NAME
        if plugin_name in self._plugins:
            logger.warning(f"Plugin {plugin_name} already registered, overwriting")
        
        self._plugins[plugin_name] = plugin_class
        logger.debug(f"Registered plugin: {plugin_name}")
    
    def get_plugin(self, plugin_name: str, config: Dict[str, Any]) -> ServerPlugin:
        """
        Get an instance of a plugin with the given configuration.
        
        Args:
            plugin_name: Name of the plugin
            config: Configuration dictionary for the plugin
        
        Returns:
            ServerPlugin instance
        
        Raises:
            KeyError: If plugin not found
        """
        if plugin_name not in self._plugins:
            raise KeyError(f"Plugin '{plugin_name}' not found")
        
        # Always create a new instance with the provided config
        # This ensures each test/request gets a fresh instance with the correct config
        # (Previously cached instances could have stale config values)
        plugin_class = self._plugins[plugin_name]
        return plugin_class(config)
    
    def list_plugins(self) -> List[Dict[str, Any]]:
        """
        List all registered plugins with their metadata and capabilities.
        
        Returns:
            List of plugin info dictionaries including capabilities with UI schema
        """
        return [
            {
                "name": name,
                "version": plugin_class.PLUGIN_VERSION,
                "supported_categories": [cat.value for cat in plugin_class.SUPPORTED_CATEGORIES],
                "config_template": plugin_class.CONFIG_TEMPLATE,
                "capabilities": [c.to_dict() for c in getattr(plugin_class, "CAPABILITIES", [])],
            }
            for name, plugin_class in self._plugins.items()
        ]

    def get_plugin_class(self, plugin_name: str) -> Optional[Type[ServerPlugin]]:
        """Get the plugin class by name."""
        return self._plugins.get(plugin_name)
    
    def get_plugins_by_category(self, category: PluginCategory) -> List[str]:
        """
        Get list of plugin names that support a specific category.
        
        Args:
            category: PluginCategory to filter by
        
        Returns:
            List of plugin names
        """
        return [
            name
            for name, plugin_class in self._plugins.items()
            if category in plugin_class.SUPPORTED_CATEGORIES
        ]
    
    def get_plugin_info(self, plugin_name: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed info for a plugin including capabilities with UI schema.
        """
        if plugin_name not in self._plugins:
            return None
        plugin_class = self._plugins[plugin_name]
        return {
            "name": plugin_class.PLUGIN_NAME,
            "version": plugin_class.PLUGIN_VERSION,
            "supported_categories": [cat.value for cat in plugin_class.SUPPORTED_CATEGORIES],
            "config_template": plugin_class.CONFIG_TEMPLATE,
            "capabilities": [c.to_dict() for c in getattr(plugin_class, "CAPABILITIES", [])],
        }

    def plugin_supports_category(self, plugin_name: str, category: PluginCategory) -> bool:
        """
        Check if a plugin supports a specific category.
        
        Args:
            plugin_name: Name of the plugin
            category: Category to check
        
        Returns:
            True if plugin supports the category
        """
        if plugin_name not in self._plugins:
            return False
        
        return category in self._plugins[plugin_name].SUPPORTED_CATEGORIES


# Global registry instance
_registry: Optional[PluginRegistry] = None


def get_registry() -> PluginRegistry:
    """Get the global plugin registry instance"""
    global _registry
    if _registry is None:
        _registry = PluginRegistry()
        # Auto-discover plugins on first access
        _registry.discover_plugins()
    return _registry

