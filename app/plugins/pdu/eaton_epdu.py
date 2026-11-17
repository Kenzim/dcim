"""Eaton ePDU (metered input) plugin: read-only SNMP over EATON-EPDU-MIB."""
from typing import Any, Dict, List, Optional

from app.plugins.pdu.base import PduCapability, PduPlugin
from app.plugins.pdu.snmp import SNMP_CONFIG_PROPERTIES, SnmpClient, SnmpError, scaled

_EPDU = "1.3.6.1.4.1.534.6.6.7"
_UNIT = f"{_EPDU}.1.2.1"
_PART_NUMBER = f"{_UNIT}.3.0"
_SERIAL_NUMBER = f"{_UNIT}.4.0"
_FIRMWARE_VERSION = f"{_UNIT}.5.0"
_TEMPERATURE_SCALE = f"{_UNIT}.9.0"  # 0 Celsius, 1 Fahrenheit
_OUTLET_COUNT = f"{_UNIT}.22.0"
_INPUT_VOLTAGE = f"{_EPDU}.3.2.1.3.0.1.1"  # millivolts
_INPUT_CURRENT = f"{_EPDU}.3.3.1.4.0.1.1"  # milliamps
_INPUT_APPARENT_POWER = f"{_EPDU}.3.4.1.3.0.1.1"  # volt-amperes
_INPUT_ACTIVE_POWER = f"{_EPDU}.3.4.1.4.0.1.1"  # watts
_INPUT_ENERGY = f"{_EPDU}.3.4.1.5.0.1.1"  # watt-hours
_GROUP_NAME = f"{_EPDU}.5.1.1.3.0"
_GROUP_RATED_CURRENT = f"{_EPDU}.5.4.1.2.0"  # milliamps, negative when unknown
_GROUP_CURRENT = f"{_EPDU}.5.4.1.3.0"  # milliamps
_TEMPERATURE_NAME = f"{_EPDU}.7.1.1.2.0"
_TEMPERATURE_STATUS = f"{_EPDU}.7.1.1.3.0"  # 1 when the probe is connected
_TEMPERATURE_VALUE = f"{_EPDU}.7.1.1.4.0"  # tenths of a degree in the unit's scale
_HUMIDITY_NAME = f"{_EPDU}.7.2.1.2.0"
_HUMIDITY_STATUS = f"{_EPDU}.7.2.1.3.0"  # 1 when the probe is connected
_HUMIDITY_VALUE = f"{_EPDU}.7.2.1.4.0"  # tenths of a percent relative humidity

_BANK_INDEXES = (1, 2)
_BANK_LETTERS = {1: "A", 2: "B"}
_PROBE_INDEXES = (1, 2)
_SNMP_DEFAULTS = {"snmp_version": "1", "auth_protocol": "MD5", "priv_protocol": "DES"}

_READING_OIDS = [
    _INPUT_VOLTAGE,
    _INPUT_CURRENT,
    _INPUT_ACTIVE_POWER,
    _INPUT_APPARENT_POWER,
]
_OPTIONAL_READING_OIDS = [
    _INPUT_ENERGY,
    _TEMPERATURE_SCALE,
    *(f"{base}.{i}" for i in _BANK_INDEXES for base in (_GROUP_NAME, _GROUP_RATED_CURRENT, _GROUP_CURRENT)),
    *(
        f"{base}.{i}"
        for i in _PROBE_INDEXES
        for base in (_TEMPERATURE_NAME, _TEMPERATURE_STATUS, _TEMPERATURE_VALUE, _HUMIDITY_NAME, _HUMIDITY_STATUS, _HUMIDITY_VALUE)
    ),
]


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _integer(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _milliamps(value: Any) -> Optional[float]:
    number = _integer(value)
    if number is None or number < 0:
        return None
    return scaled(number, 1000)


def _probes(
    values: Dict[str, Any],
    kind: str,
    name_oid: str,
    status_oid: str,
    value_oid: str,
    divisor: float,
    unit: str,
) -> List[Dict[str, Any]]:
    probes = []
    for index in _PROBE_INDEXES:
        if _integer(values.get(f"{status_oid}.{index}")) != 1:
            continue
        value = scaled(values.get(f"{value_oid}.{index}"), divisor)
        if value is None:
            continue
        probes.append({
            "name": _text(values.get(f"{name_oid}.{index}")) or f"{kind.capitalize()} {index}",
            "type": kind,
            "value": value,
            "unit": unit,
        })
    return probes


class EatonEpduPlugin(PduPlugin):
    PLUGIN_NAME = "eaton_epdu"
    PLUGIN_VERSION = "1.0.0"
    DISPLAY_NAME = "Eaton ePDU (SNMP)"
    SUPPORTED_CAPABILITIES = [PduCapability.METERING]
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            **SNMP_CONFIG_PROPERTIES,
            "snmp_version": {**SNMP_CONFIG_PROPERTIES["snmp_version"], "default": "1"},
            "auth_protocol": {**SNMP_CONFIG_PROPERTIES["auth_protocol"], "default": "MD5"},
            "priv_protocol": {**SNMP_CONFIG_PROPERTIES["priv_protocol"], "default": "DES"},
        },
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._snmp: Optional[SnmpClient] = None

    def _client(self) -> SnmpClient:
        if self._snmp is None:
            self._snmp = SnmpClient({**_SNMP_DEFAULTS, **self.config})
        return self._snmp

    async def _fetch(self, required: List[str], optional: List[str]) -> Dict[str, Any]:
        client = self._client()
        values = await client.get(required)
        values.update(await self._get_optional(client, optional))
        return values

    async def _get_optional(self, client: SnmpClient, oids: List[str]) -> Dict[str, Any]:
        # SNMP v1 fails the whole request when one OID is missing, so retry each OID alone.
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

    async def test_connection(self) -> Dict[str, Any]:
        try:
            values = await self._client().get([_PART_NUMBER, _SERIAL_NUMBER])
        except ValueError as exc:
            return {"success": False, "message": str(exc), "details": {"error_type": "configuration"}}
        except SnmpError as exc:
            return {"success": False, "message": f"SNMP error: {exc}", "details": {"error_type": "snmp"}}
        model = _text(values.get(_PART_NUMBER))
        if model is None:
            return {
                "success": False,
                "message": "Device did not return an Eaton ePDU part number",
                "details": {"error_type": "device"},
            }
        return {
            "success": True,
            "message": f"Connected to Eaton ePDU {model}",
            "details": {"model": model, "serial_number": _text(values.get(_SERIAL_NUMBER))},
        }

    async def get_pdu_info(self) -> Dict[str, Any]:
        values = await self._fetch([_PART_NUMBER, _SERIAL_NUMBER], [_FIRMWARE_VERSION, _OUTLET_COUNT])
        return {
            "model": _text(values.get(_PART_NUMBER)),
            "serial_number": _text(values.get(_SERIAL_NUMBER)),
            "firmware_version": _text(values.get(_FIRMWARE_VERSION)),
            "inlet_count": 1,
            "outlet_count": _integer(values.get(_OUTLET_COUNT)) or 0,
            "inlets": [{"position": 1, "name": "Input 1"}],
            "outlet_metering": False,
            "outlet_switching": False,
        }

    async def get_readings(self) -> Dict[str, Any]:
        values = await self._fetch(_READING_OIDS, _OPTIONAL_READING_OIDS)
        voltage = scaled(values.get(_INPUT_VOLTAGE), 1000)
        current = scaled(values.get(_INPUT_CURRENT), 1000)
        power = scaled(values.get(_INPUT_ACTIVE_POWER), 1)
        apparent = scaled(values.get(_INPUT_APPARENT_POWER), 1)
        energy = scaled(values.get(_INPUT_ENERGY), 1000)

        banks = []
        for index in _BANK_INDEXES:
            banks.append({
                "name": _text(values.get(f"{_GROUP_NAME}.{index}")) or _BANK_LETTERS[index],
                "current_a": _milliamps(values.get(f"{_GROUP_CURRENT}.{index}")),
                "rated_current_a": _milliamps(values.get(f"{_GROUP_RATED_CURRENT}.{index}")),
                "power_w": None,
            })

        temperature_unit = "°F" if _integer(values.get(_TEMPERATURE_SCALE)) == 1 else "°C"
        sensors = _probes(values, "temperature", _TEMPERATURE_NAME, _TEMPERATURE_STATUS, _TEMPERATURE_VALUE, 10, temperature_unit)
        sensors += _probes(values, "humidity", _HUMIDITY_NAME, _HUMIDITY_STATUS, _HUMIDITY_VALUE, 10, "%RH")

        return {
            "inlets": [{
                "position": 1,
                "voltage_v": voltage,
                "current_a": current,
                "power_w": power,
                "apparent_power_va": apparent,
                "energy_kwh": energy,
                "active": True,
            }],
            "banks": banks,
            "outlets": [],
            "totals": {
                "current_a": current,
                "power_w": power,
                "apparent_power_va": apparent,
                "energy_kwh": energy,
            },
            "sensors": sensors,
        }
