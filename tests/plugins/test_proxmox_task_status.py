"""Proxmox UPID task polling must use the node encoded in the UPID."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.plugins.proxmox import ProxmoxPlugin, proxmox_node_from_upid


def _make_plugin(node: str = "hv-07"):
    return ProxmoxPlugin(
        {
            "hostname": "10.16.251.1",
            "username": "root@pam",
            "password": "secret",
            "port": 8006,
            "node": node,
            "vmid": 200000,
            "verify_ssl": False,
        }
    )


def test_proxmox_node_from_upid_extracts_node():
    upid = "UPID:hv-01:003DAB3E:11BC949FE:6A6A9780:qmclone:70116:root@pam:"
    assert proxmox_node_from_upid(upid) == "hv-01"
    assert proxmox_node_from_upid("") is None
    assert proxmox_node_from_upid("not-a-upid") is None


@pytest.mark.asyncio
async def test_get_proxmox_task_status_polls_upid_node_not_plugin_node():
    plugin = _make_plugin(node="hv-07")
    upid = "UPID:hv-01:003DAB3E:11BC949FE:6A6A9780:qmclone:70116:root@pam:"

    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"data": {"status": "stopped", "exitstatus": "OK"}}

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.get = AsyncMock(return_value=fake_response)

    with patch.object(plugin, "_get_headers", AsyncMock(return_value={})):
        with patch("httpx.AsyncClient", return_value=fake_client):
            data = await plugin.get_proxmox_task_status(upid)

    assert data["exitstatus"] == "OK"
    url = fake_client.get.await_args.args[0]
    assert "/nodes/hv-01/tasks/" in url
    assert "/nodes/hv-07/tasks/" not in url
