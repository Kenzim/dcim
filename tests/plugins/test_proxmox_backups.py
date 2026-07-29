"""Tests for Proxmox plugin backup list/create/delete/restore helpers."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.plugins.proxmox import ProxmoxPlugin


def _make_plugin():
    return ProxmoxPlugin(
        {
            "hostname": "pve.example",
            "username": "root@pam",
            "password": "secret",
            "port": 8006,
            "node": "pve",
            "vmid": 101,
            "verify_ssl": False,
        }
    )


def _fake_httpx_client(json_data, status_code=200):
    fake_response = MagicMock()
    fake_response.status_code = status_code
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = json_data

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=fake_response)
    fake_client.get = AsyncMock(return_value=fake_response)
    fake_client.delete = AsyncMock(return_value=fake_response)
    return fake_client


@pytest.mark.asyncio
async def test_list_backups_filters_by_vmid():
    plugin = _make_plugin()
    plugin.ticket = "t"
    plugin.csrf_token = "c"
    fake_client = _fake_httpx_client(
        {
            "data": [
                {"volid": "pbs:backup/vm/101/a", "vmid": 101, "ctime": 1, "size": 10},
                {"volid": "pbs:backup/vm/102/b", "vmid": 102, "ctime": 2, "size": 20},
            ]
        }
    )
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        rows = await plugin.list_backups("pbs", vmid=101)
    assert len(rows) == 1
    assert rows[0]["volid"] == "pbs:backup/vm/101/a"


@pytest.mark.asyncio
async def test_extract_backup_config_gets_vzdump_extractconfig():
    plugin = _make_plugin()
    plugin.ticket = "t"
    plugin.csrf_token = "c"
    fake_client = _fake_httpx_client({"data": "smbios1: uuid=ABC,sku=rf1:tpl=debian-13;svc=1\n"})
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        text = await plugin.extract_backup_config("pbs:backup/vm/101/2026-01-01T00:00:00Z")
    assert "smbios1:" in text
    fake_client.get.assert_awaited()
    args, kwargs = fake_client.get.await_args
    assert args[0].endswith("/nodes/pve/vzdump/extractconfig")
    assert kwargs["params"]["volume"] == "pbs:backup/vm/101/2026-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_create_backup_posts_vzdump():
    plugin = _make_plugin()
    plugin.ticket = "t"
    plugin.csrf_token = "c"
    fake_client = _fake_httpx_client({"data": "UPID:pve:000:vzdump"})
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        upid = await plugin.create_backup(101, storage="pbs-rf-client", notes="n1")
    assert upid == "UPID:pve:000:vzdump"
    _args, kwargs = fake_client.post.call_args
    assert kwargs["data"]["storage"] == "pbs-rf-client"
    assert kwargs["data"]["vmid"] == 101
    # Free-text notes go via notes-template (not the rejected "notes" property)
    assert kwargs["data"]["notes-template"] == "n1"
    assert "notes" not in kwargs["data"]
    assert "compress" not in kwargs["data"]


@pytest.mark.asyncio
async def test_restore_backup_force():
    plugin = _make_plugin()
    plugin.ticket = "t"
    plugin.csrf_token = "c"
    fake_client = _fake_httpx_client({"data": "UPID:restore"})
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        upid = await plugin.restore_backup(
            archive="pbs-rf-client:backup/vm/101/snap",
            vmid=101,
            force=True,
        )
    assert upid == "UPID:restore"
    _args, kwargs = fake_client.post.call_args
    assert kwargs["data"]["force"] == 1
    assert kwargs["data"]["archive"].startswith("pbs-rf-client:")


@pytest.mark.asyncio
async def test_get_next_vmid():
    plugin = _make_plugin()
    plugin.ticket = "t"
    plugin.csrf_token = "c"
    fake_client = _fake_httpx_client({"data": 200010})
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        assert await plugin.get_next_vmid() == 200010


def test_backup_content_url_uses_full_volid():
    url = ProxmoxPlugin._backup_content_url(
        "https://pve.example:8006",
        "pve",
        "pbs-rf-client",
        "backup/vm/101/2026-07-27T01:43:19Z",
    )
    assert "/storage/pbs-rf-client/content/pbs-rf-client:backup/vm/101/" in url
    assert "backup%2Fvm" not in url


@pytest.mark.asyncio
async def test_delete_backup_uses_full_volid():
    plugin = _make_plugin()
    plugin.ticket = "t"
    plugin.csrf_token = "c"
    fake_client = _fake_httpx_client({"data": None})
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        await plugin.delete_backup(
            "pbs-rf-client",
            "pbs-rf-client:backup/vm/101/2026-07-27T01:43:19Z",
        )
    args, _kwargs = fake_client.delete.call_args
    assert "pbs-rf-client:backup/vm/101/" in args[0]
    assert "backup%2Fvm" not in args[0]
