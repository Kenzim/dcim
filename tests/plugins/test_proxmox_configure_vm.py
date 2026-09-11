"""Proxmox configure_vm applies QEMU config and reports HTTP success."""
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
async def test_configure_vm_noop_when_payload_empty():
    plugin = _make_plugin()
    with patch("httpx.AsyncClient") as mock_client_cls:
        ok = await plugin.configure_vm({"vmid": 200000}, {})
    assert ok is True
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_configure_vm_returns_http_success():
    plugin = _make_plugin()

    put_response = MagicMock()
    put_response.raise_for_status = MagicMock()
    put_response.is_success = True

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.put = AsyncMock(return_value=put_response)

    with patch.object(plugin, "_get_headers", AsyncMock(return_value={"Authorization": "x"})):
        with patch("httpx.AsyncClient", return_value=fake_client):
            ok = await plugin.configure_vm({"vmid": 200000}, {"memory_mb": 4096, "cores": 2})

    assert ok is True
    fake_client.put.assert_awaited_once()
    assert fake_client.put.await_args.kwargs["data"] == {"memory": 4096, "cores": 2}
