"""Proxmox ensure_network_bridge rewrites net0 bridge while keeping model/MAC."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
async def test_ensure_network_bridge_rewrites_bridge_keeps_model_mac():
    plugin = _make_plugin()
    current = "vmxnet3=BC:24:11:68:1B:A7,bridge=vmbr1,firewall=0"

    put_response = MagicMock()
    put_response.raise_for_status = MagicMock()
    put_response.json.return_value = {"data": None}

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.put = AsyncMock(return_value=put_response)

    with patch.object(plugin, "get_qemu_config", AsyncMock(return_value={"net0": current})):
        with patch.object(plugin, "_get_headers", AsyncMock(return_value={})):
            with patch("httpx.AsyncClient", return_value=fake_client):
                result = await plugin.ensure_network_bridge("vmbr750", vmid=200000)

    assert result["changed"] is True
    assert result["value"] == "vmxnet3=BC:24:11:68:1B:A7,firewall=0,bridge=vmbr750"
    assert fake_client.put.await_args.kwargs["data"]["net0"] == result["value"]


@pytest.mark.asyncio
async def test_ensure_network_bridge_noop_when_already_set():
    plugin = _make_plugin()
    current = "vmxnet3=BC:24:11:68:1B:A7,bridge=vmbr750,firewall=0"
    with patch.object(plugin, "get_qemu_config", AsyncMock(return_value={"net0": current})):
        result = await plugin.ensure_network_bridge("vmbr750", vmid=200000)
    assert result["changed"] is False
