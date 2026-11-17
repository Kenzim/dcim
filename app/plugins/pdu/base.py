"""
Base class for PDU (power distribution unit) plugins.

A plugin describes the unit (model, inlets, outlets, what it can do) and returns readings.
Outlet switching is optional and only offered when the plugin declares OUTLET_CONTROL.
"""
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional


class PduCapability(str, Enum):
    METERING = "metering"  # Unit, inlet or bank readings (current, power, voltage)
    OUTLET_METERING = "outlet_metering"  # Per-outlet current / power
    OUTLET_CONTROL = "outlet_control"  # Switch outlets on, off and power-cycle


OUTLET_ACTIONS = ("on", "off", "cycle")


class PduPlugin(ABC):
    """
    Subclasses set PLUGIN_NAME, PLUGIN_VERSION, DISPLAY_NAME, SUPPORTED_CAPABILITIES and
    CONFIG_TEMPLATE (JSON schema for the settings form; fields with "format": "password"
    are masked in API responses).

    get_pdu_info() returns:
        {
            "model": str | None, "serial_number": str | None, "firmware_version": str | None,
            "inlet_count": int, "outlet_count": int,
            "inlets": [{"position": 1, "name": "Input 1"}, ...],            # optional
            "outlets": [{"position": 1, "name": "Outlet 1", "bank": "A"}],  # optional
            "outlet_metering": bool, "outlet_switching": bool,
        }

    get_readings() returns (any value may be None or missing when not measured):
        {
            "inlets": [{"position": 1, "voltage_v", "current_a", "power_w",
                        "apparent_power_va", "energy_kwh", "active": bool | None}],
            "banks": [{"name": "A", "current_a", "rated_current_a", "power_w"}],
            "outlets": [{"position": 1, "current_a", "power_w", "state": "on" | "off" | None}],
            "totals": {"current_a", "power_w", "apparent_power_va", "energy_kwh"},
            "sensors": [{"name", "type": "temperature" | "humidity", "value", "unit"}],
        }
    """

    PLUGIN_NAME: str = ""
    PLUGIN_VERSION: str = ""
    DISPLAY_NAME: str = ""
    SUPPORTED_CAPABILITIES: List[PduCapability] = []
    CONFIG_TEMPLATE: Dict[str, Any] = {}

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}

    @classmethod
    def plugin_info(cls) -> Dict[str, Any]:
        return {
            "name": cls.PLUGIN_NAME,
            "display_name": cls.DISPLAY_NAME or cls.PLUGIN_NAME,
            "version": cls.PLUGIN_VERSION,
            "capabilities": [c.value for c in cls.SUPPORTED_CAPABILITIES],
            "config_template": cls.CONFIG_TEMPLATE,
        }

    @classmethod
    def supports(cls, capability: PduCapability) -> bool:
        return capability in cls.SUPPORTED_CAPABILITIES

    @abstractmethod
    async def test_connection(self) -> Dict[str, Any]:
        """Return {"success": bool, "message": str, "details": dict}."""

    @abstractmethod
    async def get_pdu_info(self) -> Dict[str, Any]:
        """Describe the unit; see the class docstring."""

    async def get_readings(self) -> Dict[str, Any]:
        raise NotImplementedError("Metering is not supported by this plugin")

    async def set_outlet_state(self, position: int, action: str) -> Dict[str, Any]:
        """Switch one outlet. action is one of OUTLET_ACTIONS."""
        raise NotImplementedError("Outlet control is not supported by this plugin")


def secret_fields(config_template: Optional[Dict[str, Any]]) -> List[str]:
    """Names of settings marked "format": "password" in a plugin CONFIG_TEMPLATE."""
    props = (config_template or {}).get("properties") or {}
    return [k for k, v in props.items() if isinstance(v, dict) and v.get("format") == "password"]
