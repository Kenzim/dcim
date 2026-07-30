from unittest.mock import AsyncMock, patch

import pytest

from app.plugins.base import PowerState
from app.plugins.ipmi import IPMIPlugin


@pytest.fixture
def plugin():
    with patch("app.plugins.ipmi.shutil.which", return_value="/usr/bin/ipmitool"):
        return IPMIPlugin({"hostname": "bmc", "username": "admin", "password": "secret"})


@pytest.mark.parametrize("value, expected", [
    ("network", "pxe"), ("PXE", "pxe"), ("disk", "disk"), ("hdd", "disk"),
    ("hd", "disk"), ("cd", "cdrom"), ("dvd", "cdrom"), ("bios", "bios"),
    ("none", "none"), (" custom ", "custom"), ("", ""),
])
def test_normalize_boot_device_aliases(plugin, value, expected):
    assert plugin._normalize_boot_device(value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("output, expected_device, persistent, uefi", [
    (b"Force PXE\nOptions apply to all future boots\nBIOS EFI boot", "pxe", True, True),
    (b"Force boot from default hard-drive\nBIOS PC compatible (legacy) boot", "disk", False, False),
    (b"Force boot from CD/DVD", "cdrom", False, None),
    (b"Force boot into BIOS setup", "bios", False, None),
    (b"Force boot from diagnostic partition", "diag", False, None),
    (b"Force boot from safe mode drive", "safe", False, None),
    (b"Force boot from floppy", "floppy", False, None),
    (b"unrecognised", None, False, None),
])
async def test_get_boot_order_parses_ipmitool_variants(plugin, output, expected_device, persistent, uefi):
    plugin._run_ipmitool = AsyncMock(return_value=(output, b"", 0))
    result = await plugin.get_boot_order()
    assert result["current_device"] == expected_device
    assert result["persistent"] is persistent
    assert result["uefi"] is uefi


@pytest.mark.asyncio
async def test_get_boot_order_raises_with_stderr(plugin):
    plugin._run_ipmitool = AsyncMock(return_value=(b"", b"unsupported", 1))
    with pytest.raises(RuntimeError, match="unsupported"):
        await plugin.get_boot_order()


@pytest.mark.asyncio
@pytest.mark.parametrize("device, persistent, uefi, command", [
    ("network", False, None, "chassis bootdev pxe"),
    ("disk", True, None, "chassis bootdev disk options=persistent"),
    ("cd", False, True, "chassis bootdev cdrom options=efiboot"),
    ("bios", True, True, "chassis bootdev bios options=persistent,efiboot"),
])
async def test_set_next_boot_device_builds_expected_command(plugin, device, persistent, uefi, command):
    plugin._run_ipmitool = AsyncMock(return_value=(b"", b"", 0))
    assert await plugin.set_next_boot_device(device, persistent=persistent, uefi=uefi) is True
    plugin._run_ipmitool.assert_awaited_once_with(command)


@pytest.mark.asyncio
async def test_set_next_boot_device_rejects_unknown_and_reports_command_failure(plugin):
    with pytest.raises(ValueError, match="Unsupported"):
        await plugin.set_next_boot_device("tape")
    plugin._run_ipmitool = AsyncMock(return_value=(b"", b"nope", 1))
    assert await plugin.set_next_boot_device("pxe") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("output, rc, expected", [
    (b"Chassis Power is on", 0, PowerState.ON),
    (b"Chassis Power is off", 0, PowerState.OFF),
    (b"unknown", 0, PowerState.UNKNOWN),
    (b"", 1, PowerState.UNKNOWN),
])
async def test_get_power_state_maps_all_command_results(plugin, output, rc, expected):
    plugin._run_ipmitool = AsyncMock(return_value=(output, b"err", rc))
    assert await plugin.get_power_state() == expected


@pytest.mark.asyncio
async def test_power_off_retries_hard_off_and_set_boot_order_uses_first_device(plugin):
    plugin._run_ipmitool = AsyncMock(side_effect=[(b"", b"soft failed", 1), (b"", b"", 0)])
    assert await plugin.power_off() is True
    assert [call.args[0] for call in plugin._run_ipmitool.await_args_list] == ["power soft", "power off"]
    plugin._run_ipmitool = AsyncMock(return_value=(b"", b"", 0))
    assert await plugin.set_boot_order(["network", "disk"]) is True
    plugin._run_ipmitool.assert_awaited_once_with("chassis bootdev pxe options=persistent")


@pytest.mark.asyncio
async def test_set_boot_order_rejects_empty_list(plugin):
    with pytest.raises(ValueError, match="cannot be empty"):
        await plugin.set_boot_order([])
