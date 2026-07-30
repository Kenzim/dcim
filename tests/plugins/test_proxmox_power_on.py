"""power_on must wait for the Proxmox start UPID and confirm running state."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.plugins.base import PowerState
from app.plugins.proxmox import ProxmoxPlugin


def _make_plugin():
    return ProxmoxPlugin(
        {
            "hostname": "10.16.251.1",
            "username": "root@pam",
            "password": "secret",
            "port": 8006,
            "node": "hv-07",
            "vmid": 200000,
            "verify_ssl": False,
        }
    )


@pytest.mark.asyncio
async def test_power_on_waits_for_upid_and_requires_running():
    plugin = _make_plugin()
    upid = "UPID:hv-07:0001:0002:0003:qmstart:200000:root@pam:"

    start_response = MagicMock()
    start_response.raise_for_status = MagicMock()
    start_response.json.return_value = {"data": upid}

    with patch.object(plugin, "get_power_state", AsyncMock(side_effect=[PowerState.OFF, PowerState.ON])):
        with patch.object(plugin, "_post_with_relocate", AsyncMock(return_value=start_response)) as post:
            with patch.object(plugin, "wait_for_proxmox_task", AsyncMock()) as wait:
                ok = await plugin.power_on()

    assert ok is True
    post.assert_awaited_once()
    wait.assert_awaited_once_with(upid, timeout=120.0)


@pytest.mark.asyncio
async def test_power_on_raises_when_start_task_fails():
    plugin = _make_plugin()
    upid = "UPID:hv-07:0001:0002:0003:qmstart:200000:root@pam:"

    start_response = MagicMock()
    start_response.raise_for_status = MagicMock()
    start_response.json.return_value = {"data": upid}

    with patch.object(plugin, "get_power_state", AsyncMock(return_value=PowerState.OFF)):
        with patch.object(plugin, "_post_with_relocate", AsyncMock(return_value=start_response)):
            with patch.object(
                plugin,
                "wait_for_proxmox_task",
                AsyncMock(side_effect=RuntimeError("Proxmox task failed (bridge missing)")),
            ):
                with pytest.raises(RuntimeError, match="bridge missing"):
                    await plugin.power_on()
