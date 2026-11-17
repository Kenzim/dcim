import pytest

from app.plugins.pdu import tripplite as mod
from app.plugins.pdu.base import PduCapability
from app.plugins.pdu.snmp import SnmpClient, SnmpError
from app.plugins.pdu.tripplite import TrippLiteAtsPlugin

OUTLET_STATES = [2] * 19


def config(**extra):
    value = {"hostname": "ats", "username": "monitor"}
    value.update(extra)
    return value


def device_values(**overrides):
    values = {
        mod._MODEL: "PDUMH30HVATNET",
        mod._INPUT_COUNT: 2,
        mod._OUTLET_COUNT: 19,
        mod._INPUT1_VOLTAGE: 2018,
        mod._OUTPUT_CURRENT: 1811,
        mod._OUTPUT_POWER: 3642,
        f"{mod._CIRCUIT_CURRENT}.1.1": 874,
        f"{mod._CIRCUIT_CURRENT}.1.2": 937,
        f"{mod._CIRCUIT_CURRENT}.1.3": 0,
        f"{mod._CIRCUIT_POWER}.1.1": 1755,
        f"{mod._CIRCUIT_POWER}.1.2": 1887,
        f"{mod._CIRCUIT_POWER}.1.3": 0,
    }
    values.update(overrides)
    return values


def walk_tables(outlet_names=None, outlet_states=None):
    names = outlet_names if outlet_names is not None else [f"Load{n}" for n in range(1, 20)]
    states = outlet_states if outlet_states is not None else OUTLET_STATES
    return {
        mod._OUTLET_NAME: [(f"{mod._OUTLET_NAME}.1.{n}", name) for n, name in enumerate(names, start=1)],
        mod._OUTLET_STATE: [(f"{mod._OUTLET_STATE}.1.{n}", state) for n, state in enumerate(states, start=1)],
    }


def patch_device(monkeypatch, values, tables=None, strict=False):
    """Replace SnmpClient.get and walk with a device. A strict device fails the whole request
    when any requested OID is absent (or a walked table is absent), as SNMP v1 does."""
    requests = []
    tables = walk_tables() if tables is None else tables

    async def get(self, oids):
        oids = list(oids)
        requests.append(oids)
        if strict and any(oid not in values for oid in oids):
            raise SnmpError("noSuchName")
        return {oid: values.get(oid) for oid in oids}

    async def walk(self, base_oid, max_rows=2000):
        if base_oid not in tables:
            raise SnmpError("noSuchName")
        return tables[base_oid]

    monkeypatch.setattr(SnmpClient, "get", get)
    monkeypatch.setattr(SnmpClient, "walk", walk)
    return requests


def test_capabilities_are_metering_only():
    assert TrippLiteAtsPlugin.SUPPORTED_CAPABILITIES == [PduCapability.METERING]
    assert not TrippLiteAtsPlugin.supports(PduCapability.OUTLET_CONTROL)
    assert not TrippLiteAtsPlugin.supports(PduCapability.OUTLET_METERING)


def test_form_defaults_match_the_unit():
    props = TrippLiteAtsPlugin.CONFIG_TEMPLATE["properties"]
    assert props["snmp_version"]["default"] == "3"
    assert props["security_level"]["default"] == "authPriv"
    assert props["auth_protocol"]["default"] == "SHA"
    assert props["priv_protocol"]["default"] == "AES128"


@pytest.mark.asyncio
async def test_config_without_version_uses_snmp_v3(monkeypatch):
    patch_device(monkeypatch, device_values())
    plugin = TrippLiteAtsPlugin(config())
    await plugin.get_readings()
    assert plugin._snmp.version == "3"


@pytest.mark.asyncio
async def test_set_outlet_state_is_not_implemented():
    with pytest.raises(NotImplementedError):
        await TrippLiteAtsPlugin(config()).set_outlet_state(1, "off")


@pytest.mark.asyncio
async def test_readings_from_the_unit(monkeypatch):
    patch_device(monkeypatch, device_values())
    readings = await TrippLiteAtsPlugin(config()).get_readings()

    assert readings["inlets"] == [
        {
            "position": 1,
            "voltage_v": 201.8,
            "current_a": None,
            "power_w": None,
            "apparent_power_va": None,
            "energy_kwh": None,
            "active": None,
        },
        {
            "position": 2,
            "voltage_v": None,
            "current_a": None,
            "power_w": None,
            "apparent_power_va": None,
            "energy_kwh": None,
            "active": None,
        },
    ]
    assert readings["banks"] == [
        {"name": "Bank 1", "current_a": 8.74, "rated_current_a": None, "power_w": 1755.0},
        {"name": "Bank 2", "current_a": 9.37, "rated_current_a": None, "power_w": 1887.0},
        {"name": "Bank 3", "current_a": 0.0, "rated_current_a": None, "power_w": 0.0},
    ]
    assert readings["totals"] == {
        "current_a": 18.11,
        "power_w": 3642.0,
        "apparent_power_va": None,
        "energy_kwh": None,
    }
    assert readings["sensors"] == []


@pytest.mark.asyncio
async def test_outlet_states_are_read_without_metering(monkeypatch):
    patch_device(monkeypatch, device_values())
    readings = await TrippLiteAtsPlugin(config()).get_readings()

    assert len(readings["outlets"]) == 19
    assert readings["outlets"][0] == {"position": 1, "current_a": None, "power_w": None, "state": "on"}
    assert readings["outlets"][-1] == {"position": 19, "current_a": None, "power_w": None, "state": "on"}


@pytest.mark.asyncio
async def test_outlet_state_off_is_mapped(monkeypatch):
    patch_device(monkeypatch, device_values(), tables=walk_tables(outlet_states=[2] * 18 + [1]))
    readings = await TrippLiteAtsPlugin(config()).get_readings()

    assert readings["outlets"][-1] == {"position": 19, "current_a": None, "power_w": None, "state": "off"}


@pytest.mark.asyncio
async def test_missing_optional_oids_yield_none(monkeypatch):
    values = device_values()
    del values[f"{mod._CIRCUIT_CURRENT}.1.3"]
    del values[mod._INPUT1_VOLTAGE]
    patch_device(monkeypatch, values, strict=True)
    readings = await TrippLiteAtsPlugin(config()).get_readings()

    assert readings["inlets"][0]["voltage_v"] is None
    assert readings["banks"][2]["current_a"] is None
    assert readings["banks"][2]["power_w"] == 0.0
    assert readings["banks"][0]["current_a"] == 8.74
    assert readings["totals"]["current_a"] == 18.11


@pytest.mark.asyncio
async def test_missing_outlet_tables_leave_outlets_empty(monkeypatch):
    patch_device(monkeypatch, device_values(), tables={})
    readings = await TrippLiteAtsPlugin(config()).get_readings()
    info = await TrippLiteAtsPlugin(config()).get_pdu_info()

    assert readings["outlets"] == []
    assert readings["totals"]["power_w"] == 3642.0
    assert info["outlets"] == []


@pytest.mark.asyncio
async def test_readings_use_few_requests_when_the_unit_answers(monkeypatch):
    requests = patch_device(monkeypatch, device_values())
    await TrippLiteAtsPlugin(config()).get_readings()
    assert len(requests) == 1
    assert all(len(batch) <= 30 for batch in requests)


@pytest.mark.asyncio
async def test_pdu_info(monkeypatch):
    patch_device(monkeypatch, device_values())
    info = await TrippLiteAtsPlugin(config()).get_pdu_info()

    assert info["model"] == "PDUMH30HVATNET"
    assert info["serial_number"] is None
    assert info["firmware_version"] is None
    assert info["inlet_count"] == 2
    assert info["inlets"] == [{"position": 1, "name": "Input 1"}, {"position": 2, "name": "Input 2"}]
    assert info["outlet_count"] == 19
    assert info["outlets"][0] == {"position": 1, "name": "Load1"}
    assert info["outlets"][-1] == {"position": 19, "name": "Load19"}
    assert info["outlet_metering"] is False
    assert info["outlet_switching"] is False


@pytest.mark.asyncio
async def test_connection_success(monkeypatch):
    patch_device(monkeypatch, device_values())
    result = await TrippLiteAtsPlugin(config()).test_connection()
    assert result["success"]
    assert result["details"]["model"] == "PDUMH30HVATNET"


@pytest.mark.asyncio
async def test_connection_without_model(monkeypatch):
    patch_device(monkeypatch, device_values(**{mod._MODEL: None}))
    result = await TrippLiteAtsPlugin(config()).test_connection()
    assert not result["success"]


@pytest.mark.asyncio
async def test_connection_rejects_missing_hostname():
    result = await TrippLiteAtsPlugin(config(hostname="")).test_connection()
    assert not result["success"]
    assert result["details"]["error_type"] == "configuration"
