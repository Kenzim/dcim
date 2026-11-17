"""Discovers PDU plugins in app/plugins/pdu/ (every PduPlugin subclass with a PLUGIN_NAME)."""
import importlib
import inspect
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from app.plugins.pdu.base import PduCapability, PduPlugin

logger = logging.getLogger(__name__)

_SKIP_MODULES = {"__init__", "base", "registry", "snmp"}


class PduPluginRegistry:
    def __init__(self):
        self._plugins: Dict[str, Type[PduPlugin]] = {}

    def discover(self, plugin_dir: Optional[Path] = None) -> None:
        plugin_dir = plugin_dir or Path(__file__).parent
        for path in sorted(plugin_dir.glob("*.py")):
            if path.stem in _SKIP_MODULES:
                continue
            try:
                module = importlib.import_module(f"app.plugins.pdu.{path.stem}")
            except Exception:
                logger.exception("Failed to load PDU plugin module %s", path.name)
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, PduPlugin) and obj is not PduPlugin and obj.PLUGIN_NAME:
                    self.register(obj)

    def register(self, plugin_class: Type[PduPlugin]) -> None:
        self._plugins[plugin_class.PLUGIN_NAME] = plugin_class

    def has(self, name: str) -> bool:
        return name in self._plugins

    def get_class(self, name: str) -> Type[PduPlugin]:
        if name not in self._plugins:
            raise KeyError(f"PDU plugin '{name}' not found")
        return self._plugins[name]

    def get_plugin(self, name: str, config: Dict[str, Any]) -> PduPlugin:
        return self.get_class(name)(config or {})

    def supports(self, name: str, capability: PduCapability) -> bool:
        return name in self._plugins and self._plugins[name].supports(capability)

    def list_plugins(self) -> List[Dict[str, Any]]:
        return [cls.plugin_info() for cls in sorted(self._plugins.values(), key=lambda c: c.PLUGIN_NAME)]


_registry: Optional[PduPluginRegistry] = None


def get_pdu_registry() -> PduPluginRegistry:
    global _registry
    if _registry is None:
        _registry = PduPluginRegistry()
        _registry.discover()
    return _registry
