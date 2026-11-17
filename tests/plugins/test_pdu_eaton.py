import pytest

from app.plugins.pdu import eaton_epdu as mod
from app.plugins.pdu.base import PduCapability
from app.plugins.pdu.eaton_epdu import EatonEpduPlugin
from app.plugins.pdu.snmp import SnmpClient, SnmpError


def config(**extra):
    value = {"hostname": "epdu", "community": "public"}
    value.update(extra)
    return value


def device_values(**overrides):
    values = {
        mod._PART_NUMBER: "EMI104-10",
        mod._SERIAL_NUMBER: "ABC1234567",
        mod._FIRMWARE_VERSION: "01.00.0026",
        mod._OUTLET_COUNT: 0,
        mod._INPUT_VOLTAGE: 204600,
        mod._INPUT_CURRENT: 11240,
        mod._INPUT_ACTIVE_POWER: 2276,
        mod._INPUT_APPARENT_POWER: 2299,
        mod._INPUT_ENERGY: 123456,
        f"{mod._GROUP_RATED_CURRENT}.1": 16000,
        f"{mod._GROUP_RATED_CURRENT}.2": 16000,
        f"{mod._GROUP_CURRENT}.1": 3840,
        f"{mod._GROUP_CURRENT}.2": 7400,
        mod._TEMPERATURE_SCALE: 0,
        f"{mod._TEMPERATURE_STATUS}.1": 1,
        f"{mod._TEMPERATURE_VALUE}.1": 225,
        f"{mod._TEMPERATURE_STATUS}.2": 0,
        f"{mod._HUMIDITY_STATUS}.1": 1,
        f"{mod._HUMIDITY_VALUE}.1": 415,
    }
    values.update(overrides)
    return values


def patch_device(monkeypatch, values, strict=False):
    """Replace SnmpClient.get with a device holding `values`. A strict device fails the whole
    request when any requested OID is absent, as SNMP v1 does."""
    requests = []

    async def get(self, oids):
        oids = list(oids)
        requests.append(oids)
        if strict and any(oid not in values for oid in oids):
            raise SnmpError("noSuchName")
        return {oid: values.get(oid) for oid in oids}

    monkeypatch.setattr(SnmpClient, "get", get)
    return requests


def test_capabilities_are_metering_only():
    assert EatonEpduPlugin.SUPPORTED_CAPABILITIES == [PduCapability.METERING]
    assert not EatonEpduPlugin.supports(PduCapability.OUTLET_CONTROL)
    assert not EatonEpduPlugin.supports(PduCapability.OUTLET_METERING)


def test_form_defaults_match_the_unit():
    props = EatonEpduPlugin.CONFIG_TEMPLATE["properties"]
    assert props["snmp_version"]["default"] == "1"
    assert props["auth_protocol"]["default"] == "MD5"
    assert props["priv_protocol"]["default"] == "DES"


@pytest.mark.asyncio
async def test_config_without_version_uses_snmp_v1(monkeypatch):
    patch_device(monkeypatch, device_values())
    plugin = EatonEpduPlugin(config())
    await plugin.get_readings()
    assert plugin._snmp.version == "1"


@pytest.mark.asyncio
async def test_set_outlet_state_is_not_implemented():
    with pytest.raises(NotImplementedError):
        await EatonEpduPlugin(config()).set_outlet_state(1, "off")


@pytest.mark.asyncio
async def test_readings_from_the_unit(monkeypatch):
    patch_device(monkeypatch, device_values())
    readings = await EatonEpduPlugin(config()).get_readings()

    assert readings["inlets"] == [{
        "position": 1,
        "voltage_v": 204.6,
        "current_a": 11.24,
        "power_w": 2276.0,
        "apparent_power_va": 2299.0,
        "energy_kwh": 123.456,
        "active": True,
    }]
    assert readings["banks"] == [
        {"name": "A", "current_a": 3.84, "rated_current_a": 16.0, "power_w": None},
        {"name": "B", "current_a": 7.4, "rated_current_a": 16.0, "power_w": None},
    ]
    assert readings["totals"] == {
        "current_a": 11.24,
        "power_w": 2276.0,
        "apparent_power_va": 2299.0,
        "energy_kwh": 123.456,
    }
    assert readings["sensors"] == [
        {"name": "Temperature 1", "type": "temperature", "value": 22.5, "unit": "°C"},
        {"name": "Humidity 1", "type": "humidity", "value": 41.5, "unit": "%RH"},
    ]
    assert readings["outlets"] == []


@pytest.mark.asyncio
async def test_missing_optional_oids_yield_none(monkeypatch):
    values = device_values()
    for oid in (mod._INPUT_ENERGY, f"{mod._GROUP_CURRENT}.2", f"{mod._GROUP_RATED_CURRENT}.1"):
        del values[oid]
    patch_device(monkeypatch, values, strict=True)
    readings = await EatonEpduPlugin(config()).get_readings()

    assert readings["inlets"][0]["voltage_v"] == 204.6
    assert readings["inlets"][0]["energy_kwh"] is None
    assert readings["banks"][0] == {"name": "A", "current_a": 3.84, "rated_current_a": None, "power_w": None}
    assert readings["banks"][1]["current_a"] is None


@pytest.mark.asyncio
async def test_missing_required_oid_raises(monkeypatch):
    values = device_values()
    del values[mod._INPUT_CURRENT]
    patch_device(monkeypatch, values, strict=True)
    with pytest.raises(SnmpError):
        await EatonEpduPlugin(config()).get_readings()


@pytest.mark.asyncio
async def test_negative_rated_capacity_is_unknown(monkeypatch):
    patch_device(monkeypatch, device_values(**{f"{mod._GROUP_RATED_CURRENT}.1": -1}))
    readings = await EatonEpduPlugin(config()).get_readings()
    assert readings["banks"][0]["rated_current_a"] is None


@pytest.mark.asyncio
async def test_fahrenheit_scale_is_labelled(monkeypatch):
    patch_device(monkeypatch, device_values(**{mod._TEMPERATURE_SCALE: 1}))
    readings = await EatonEpduPlugin(config()).get_readings()
    assert readings["sensors"][0]["unit"] == "°F"


@pytest.mark.asyncio
async def test_readings_use_one_batch_when_the_unit_answers(monkeypatch):
    requests = patch_device(monkeypatch, device_values())
    await EatonEpduPlugin(config()).get_readings()
    assert len(requests) == 2
    assert all(len(batch) <= 30 for batch in requests)


@pytest.mark.asyncio
async def test_pdu_info(monkeypatch):
    patch_device(monkeypatch, device_values())
    info = await EatonEpduPlugin(config()).get_pdu_info()

    assert info["model"] == "EMI104-10"
    assert info["serial_number"] == "ABC1234567"
    assert info["firmware_version"] == "01.00.0026"
    assert info["inlet_count"] == 1
    assert info["outlet_count"] == 0
    assert info["outlet_metering"] is False
    assert info["outlet_switching"] is False


@pytest.mark.asyncio
async def test_connection_success(monkeypatch):
    patch_device(monkeypatch, device_values())
    result = await EatonEpduPlugin(config()).test_connection()
    assert result["success"]
    assert result["details"]["model"] == "EMI104-10"


@pytest.mark.asyncio
async def test_connection_without_part_number(monkeypatch):
    patch_device(monkeypatch, device_values(**{mod._PART_NUMBER: None}))
    result = await EatonEpduPlugin(config()).test_connection()
    assert not result["success"]


@pytest.mark.asyncio
async def test_connection_rejects_missing_hostname():
    result = await EatonEpduPlugin(config(hostname="")).test_connection()
    assert not result["success"]
    assert result["details"]["error_type"] == "configuration"
