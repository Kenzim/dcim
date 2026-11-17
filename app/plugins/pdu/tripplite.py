"""Tripp Lite ATS PDU plugin: read-only SNMP over TRIPPLITE-PRODUCTS (PowerAlert)."""
from typing import Any, Dict, List, Optional

from app.plugins.pdu.base import PduCapability, PduPlugin
from app.plugins.pdu.snmp import SNMP_CONFIG_PROPERTIES, SnmpClient, SnmpError, last_index, scaled

_ATS = "1.3.6.1.4.1.850.1.1.3.4"
_MODEL = "1.3.6.1.4.1.850.1.1.1.2.1.5.1"
_INPUT_COUNT = f"{_ATS}.1.2.1.1.1"
_OUTLET_COUNT = f"{_ATS}.1.2.1.4.1"
_INPUT1_VOLTAGE = f"{_ATS}.3.1.2.1.5.1.1.1"  # tenths of a volt, input line 1, phase 1
_OUTPUT_CURRENT = f"{_ATS}.3.2.1.1.5.1.1"  # hundredths of an amp
_OUTPUT_POWER = f"{_ATS}.2.1.1.9.1"  # watts
_CIRCUIT_CURRENT = f"{_ATS}.3.4.1.1.4"  # hundredths of an amp, indexed by device and circuit
_CIRCUIT_POWER = f"{_ATS}.3.4.1.1.8"  # watts, indexed by device and circuit
_OUTLET_NAME = f"{_ATS}.3.3.1.1.2"  # walked, indexed by device and outlet
_OUTLET_STATE = f"{_ATS}.3.3.1.1.4"  # walked, indexed by device and outlet; 1 off, 2 on

_BANK_INDEXES = (1, 2, 3)
_OUTLET_STATES = {1: "off", 2: "on"}
_SNMP_DEFAULTS = {
    "snmp_version": "3",
    "security_level": "authPriv",
    "auth_protocol": "SHA",
    "priv_protocol": "AES128",
}

_READING_OIDS = [
    _INPUT1_VOLTAGE,
    _OUTPUT_CURRENT,
    _OUTPUT_POWER,
    *(f"{_CIRCUIT_CURRENT}.1.{i}" for i in _BANK_INDEXES),
    *(f"{_CIRCUIT_POWER}.1.{i}" for i in _BANK_INDEXES),
]


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _integer(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _inlet(position: int, voltage: Optional[float]) -> Dict[str, Any]:
    return {
        "position": position,
        "voltage_v": voltage,
        "current_a": None,
        "power_w": None,
        "apparent_power_va": None,
        "energy_kwh": None,
        "active": None,
    }


class TrippLiteAtsPlugin(PduPlugin):
    PLUGIN_NAME = "tripplite_ats"
    PLUGIN_VERSION = "1.0.0"
    DISPLAY_NAME = "Tripp Lite PDU / ATS (SNMP)"
    SUPPORTED_CAPABILITIES = [PduCapability.METERING]
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            **SNMP_CONFIG_PROPERTIES,
            "snmp_version": {**SNMP_CONFIG_PROPERTIES["snmp_version"], "default": "3"},
            "auth_protocol": {**SNMP_CONFIG_PROPERTIES["auth_protocol"], "default": "SHA"},
            "priv_protocol": {**SNMP_CONFIG_PROPERTIES["priv_protocol"], "default": "AES128"},
        },
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._snmp: Optional[SnmpClient] = None

    def _client(self) -> SnmpClient:
        if self._snmp is None:
            self._snmp = SnmpClient({**_SNMP_DEFAULTS, **self.config})
        return self._snmp

    async def _get(self, oids: List[str]) -> Dict[str, Any]:
        # A missing OID can fail a whole SNMP request, so retry each OID alone.
        client = self._client()
        try:
            return await client.get(oids)
        except SnmpError:
            values: Dict[str, Any] = {}
            for oid in oids:
                try:
                    values.update(await client.get([oid]))
                except SnmpError:
                    values[oid] = None
            return values

    async def _walk(self, base: str) -> Dict[int, Any]:
        """Column values keyed by the outlet or circuit number (the last index part)."""
        try:
            rows = await self._client().walk(base)
        except SnmpError:
            return {}
        values: Dict[int, Any] = {}
        for oid, value in rows:
            index = last_index(oid, base)
            if index is None:
                continue
            try:
                values[int(index.rsplit(".", 1)[-1])] = value
            except ValueError:
                continue
        return values

    async def test_connection(self) -> Dict[str, Any]:
        try:
            values = await self._client().get([_MODEL])
        except ValueError as exc:
            return {"success": False, "message": str(exc), "details": {"error_type": "configuration"}}
        except SnmpError as exc:
            return {"success": False, "message": f"SNMP error: {exc}", "details": {"error_type": "snmp"}}
        model = _text(values.get(_MODEL))
        if model is None:
            return {
                "success": False,
                "message": "Device did not return a Tripp Lite model name",
                "details": {"error_type": "device"},
            }
        return {
            "success": True,
            "message": f"Connected to Tripp Lite {model}",
            "details": {"model": model},
        }

    async def get_pdu_info(self) -> Dict[str, Any]:
        values = await self._get([_MODEL, _INPUT_COUNT, _OUTLET_COUNT])
        input_count = _integer(values.get(_INPUT_COUNT)) or 0
        names = await self._walk(_OUTLET_NAME)
        return {
            "model": _text(values.get(_MODEL)),
            "serial_number": None,
            "firmware_version": None,
            "inlet_count": input_count,
            "outlet_count": _integer(values.get(_OUTLET_COUNT)) or 0,
            "inlets": [{"position": n, "name": f"Input {n}"} for n in range(1, input_count + 1)],
            "outlets": [
                {"position": n, "name": _text(name) or f"Outlet {n}"}
                for n, name in sorted(names.items())
            ],
            "outlet_metering": False,
            "outlet_switching": False,
        }

    async def get_readings(self) -> Dict[str, Any]:
        values = await self._get(_READING_OIDS)
        states = await self._walk(_OUTLET_STATE)
        current = scaled(values.get(_OUTPUT_CURRENT), 100)
        power = scaled(values.get(_OUTPUT_POWER), 1)

        return {
            "inlets": [
                _inlet(1, scaled(values.get(_INPUT1_VOLTAGE), 10)),
                _inlet(2, None),
            ],
            "banks": [
                {
                    "name": f"Bank {i}",
                    "current_a": scaled(values.get(f"{_CIRCUIT_CURRENT}.1.{i}"), 100),
                    "rated_current_a": None,
                    "power_w": scaled(values.get(f"{_CIRCUIT_POWER}.1.{i}"), 1),
                }
                for i in _BANK_INDEXES
            ],
            "outlets": [
                {
                    "position": n,
                    "current_a": None,
                    "power_w": None,
                    "state": _OUTLET_STATES.get(_integer(state)),
                }
                for n, state in sorted(states.items())
            ],
            "totals": {
                "current_a": current,
                "power_w": power,
                "apparent_power_va": None,
                "energy_kwh": None,
            },
            "sensors": [],
        }
