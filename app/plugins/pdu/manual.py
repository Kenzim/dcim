"""Manual PDU plugin: an unmanaged PDU with no network interface. Counts come from the settings."""
from typing import Any, Dict

from app.plugins.pdu.base import PduPlugin


class ManualPduPlugin(PduPlugin):
    PLUGIN_NAME = "manual"
    PLUGIN_VERSION = "1.0.0"
    DISPLAY_NAME = "Manual (unmanaged)"
    SUPPORTED_CAPABILITIES = []
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            "outlet_count": {
                "type": "integer",
                "title": "Outlets",
                "description": "Number of outlets on the PDU",
                "default": 8,
                "minimum": 0,
                "maximum": 96,
            },
            "inlet_count": {
                "type": "integer",
                "title": "Inputs",
                "description": "Number of power inputs (2 for an ATS)",
                "default": 1,
                "minimum": 1,
                "maximum": 4,
            },
        },
    }

    def _count(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    async def test_connection(self) -> Dict[str, Any]:
        return {"success": True, "message": "Manual PDU: nothing to connect to", "details": {}}

    async def get_pdu_info(self) -> Dict[str, Any]:
        return {
            "model": None,
            "serial_number": None,
            "firmware_version": None,
            "inlet_count": self._count("inlet_count", 1, 1, 4),
            "outlet_count": self._count("outlet_count", 8, 0, 96),
            "outlet_metering": False,
            "outlet_switching": False,
        }
