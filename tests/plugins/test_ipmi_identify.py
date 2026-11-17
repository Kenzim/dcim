"""IPMI plugin: chassis identify LED read and set."""

from unittest.mock import AsyncMock, patch

import pytest

from app.plugins.ipmi import IPMIPlugin, parse_identify_state, redfish_indicator_state


@pytest.fixture
def plugin():
    with patch("app.plugins.ipmi.shutil.which", return_value="/usr/bin/ipmitool"):
        return IPMIPlugin({"hostname": "bmc", "username": "admin", "password": "secret"})


@pytest.mark.parametrize("raw, expected", [
    (" 01 10 40 70\n", "off"),        # supported bit set, state 00
    (" 01 10 50 70\n", "timed"),      # state 01
    (" 01 10 60\n", "on"),            # state 10, no optional fourth byte
    (" 01 10 00 70\n", None),         # BMC does not report identify state
    (" 01 10 70\n", None),            # reserved state 11
    (" 01 10\n", None),               # too short
    ("garbage", None),
])
def test_parse_identify_state(raw, expected):
    assert parse_identify_state(raw) == expected


@pytest.mark.parametrize("resource, expected", [
    ({"LocationIndicatorActive": True}, "on"),
    ({"LocationIndicatorActive": False, "IndicatorLED": "Lit"}, "off"),
    ({"IndicatorLED": "Blinking"}, "on"),
    ({"IndicatorLED": "Off"}, "off"),
    ({"IndicatorLED": "Unknown"}, None),
    ({}, None),
])
def test_redfish_indicator_state(resource, expected):
    assert redfish_indicator_state(resource) == expected


def test_identify_capability_is_declared():
    ids = [cap.id for cap in IPMIPlugin.CAPABILITIES]
    assert "identify" in ids
    cap = next(c for c in IPMIPlugin.CAPABILITIES if c.id == "identify")
    assert cap.optional is False


@pytest.mark.asyncio
async def test_get_identify_state_from_ipmi(plugin):
    plugin._run_ipmitool = AsyncMock(return_value=(b" 01 10 60 70\n", b"", 0))
    plugin._redfish_identify_state = AsyncMock()
    assert await plugin.get_identify_state() == {"state": "on", "source": "ipmi"}
    plugin._run_ipmitool.assert_awaited_once_with("raw 0x00 0x01")
    plugin._redfish_identify_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_identify_state_falls_back_to_redfish(plugin):
    plugin._run_ipmitool = AsyncMock(return_value=(b" 01 10 00 70\n", b"", 0))
    plugin._redfish_identify_state = AsyncMock(return_value="off")
    assert await plugin.get_identify_state() == {"state": "off", "source": "redfish"}


@pytest.mark.asyncio
async def test_get_identify_state_unknown(plugin):
    plugin._run_ipmitool = AsyncMock(return_value=(b"", b"error", 1))
    plugin._redfish_identify_state = AsyncMock(return_value=None)
    assert await plugin.get_identify_state() == {"state": "unknown", "source": None}


@pytest.mark.asyncio
async def test_set_identify_on_indefinitely(plugin):
    plugin._run_ipmitool = AsyncMock(return_value=(b"Chassis identify interval: indefinite\n", b"", 0))
    result = await plugin.set_identify(True)
    plugin._run_ipmitool.assert_awaited_once_with("chassis identify force")
    assert result == {"success": True, "state": "on", "seconds": None, "message": None}


@pytest.mark.asyncio
async def test_set_identify_force_refused_falls_back_to_max_interval(plugin):
    plugin._run_ipmitool = AsyncMock(side_effect=[(b"", b"Chassis may not support Force Identify On", 1), (b"", b"", 0)])
    result = await plugin.set_identify(True)
    assert [c.args[0] for c in plugin._run_ipmitool.await_args_list] == ["chassis identify force", "chassis identify 255"]
    assert result["success"] is True
    assert result["state"] == "timed" and result["seconds"] == 255
    assert "255 seconds" in result["message"]


@pytest.mark.asyncio
async def test_set_identify_timed_and_off(plugin):
    plugin._run_ipmitool = AsyncMock(return_value=(b"", b"", 0))
    timed = await plugin.set_identify(True, 30)
    plugin._run_ipmitool.assert_awaited_with("chassis identify 30")
    assert timed["state"] == "timed" and timed["seconds"] == 30
    off = await plugin.set_identify(False, 30)
    plugin._run_ipmitool.assert_awaited_with("chassis identify 0")
    assert off["state"] == "off"


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [0, 256, -5])
async def test_set_identify_rejects_bad_interval(plugin, seconds):
    plugin._run_ipmitool = AsyncMock()
    with pytest.raises(ValueError, match="between 1 and 255"):
        await plugin.set_identify(True, seconds)
    plugin._run_ipmitool.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_identify_reports_bmc_failure(plugin):
    plugin._run_ipmitool = AsyncMock(return_value=(b"", b"Unable to establish IPMI v2 session", 1))
    result = await plugin.set_identify(False)
    assert result["success"] is False
    assert "IPMI v2 session" in result["message"]
