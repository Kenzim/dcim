"""Proxmox guest agent and helper coverage."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.plugins.base import PowerState
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


@pytest.mark.asyncio
async def test_vm_exists_true_and_false():
    plugin = _make_plugin()
    plugin.ticket = "t"
    plugin.csrf_token = "c"

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.raise_for_status = MagicMock()

    missing_response = MagicMock()
    missing_response.status_code = 404

    with patch.object(
        plugin,
        "_get_with_relocate",
        new=AsyncMock(side_effect=[ok_response, missing_response]),
    ):
        assert await plugin.vm_exists() is True
        assert await plugin.vm_exists() is False


@pytest.mark.asyncio
async def test_guest_agent_ready_handles_success_and_failure():
    plugin = _make_plugin()
    ok = MagicMock(status_code=200)
    with patch.object(plugin, "_post_with_relocate", new=AsyncMock(return_value=ok)):
        assert await plugin.guest_agent_ready() is True
    with patch.object(plugin, "_post_with_relocate", new=AsyncMock(side_effect=RuntimeError("down"))):
        assert await plugin.guest_agent_ready() is False


@pytest.mark.asyncio
async def test_guest_exec_polls_until_exit():
    plugin = _make_plugin()
    plugin.ticket = "t"
    plugin.csrf_token = "c"

    post_response = MagicMock()
    post_response.raise_for_status = MagicMock()
    post_response.json.return_value = {"data": {"pid": 7}}

    running = MagicMock()
    running.raise_for_status = MagicMock()
    running.json.return_value = {"data": {"exited": 0}}

    done = MagicMock()
    done.raise_for_status = MagicMock()
    done.json.return_value = {
        "data": {
            "exited": 1,
            "exitcode": 0,
            "out-data": "hello",
            "err-data": "",
        }
    }

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=post_response)
    fake_client.get = AsyncMock(side_effect=[running, done])
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        result = await plugin.guest_exec(["/bin/echo", "hello"], max_wait=5.0)

    assert result["exitcode"] == 0
    assert result["out-data"] == "hello"
    assert fake_client.get.await_count == 2


@pytest.mark.asyncio
async def test_guest_exec_rejects_empty_command():
    plugin = _make_plugin()
    with pytest.raises(ValueError, match="non-empty"):
        await plugin.guest_exec([])


def test_notes_template_escapes_user_input():
    escaped = ProxmoxPlugin._notes_template("line1\n{{bad}}\\tail")
    assert "\\n" in escaped
    assert "{ {" in escaped
    assert "} }" in escaped


@pytest.mark.asyncio
async def test_get_power_state_maps_qemu_status():
    plugin = _make_plugin()
    plugin.ticket = "t"
    plugin.csrf_token = "c"
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": {"status": "running"}}
    with patch.object(plugin, "_get_with_relocate", new=AsyncMock(return_value=response)):
        assert await plugin.get_power_state() == PowerState.ON

    stopped = MagicMock()
    stopped.raise_for_status = MagicMock()
    stopped.json.return_value = {"data": {"status": "stopped"}}
    with patch.object(plugin, "_get_with_relocate", new=AsyncMock(return_value=stopped)):
        assert await plugin.get_power_state() == PowerState.OFF


@pytest.mark.asyncio
async def test_power_on_short_circuits_when_already_running():
    plugin = _make_plugin()
    with patch.object(plugin, "get_power_state", new=AsyncMock(return_value=PowerState.ON)):
        assert await plugin.power_on() is True


@pytest.mark.asyncio
async def test_power_off_posts_shutdown_or_stop():
    plugin = _make_plugin()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": None}
    post_relocate = AsyncMock(return_value=response)
    with patch.object(plugin, "_post_with_relocate", post_relocate):
        assert await plugin.power_off(force=False) is True
        assert await plugin.power_off(force=True) is True
    urls = [call.args[0]() for call in post_relocate.await_args_list]
    assert any("shutdown" in url for url in urls)
    assert any("stop" in url for url in urls)


@pytest.mark.asyncio
async def test_wait_for_proxmox_task_completes_on_ok():
    plugin = _make_plugin()
    with patch.object(
        plugin,
        "get_proxmox_task_status",
        new=AsyncMock(return_value={"status": "stopped", "exitstatus": "OK"}),
    ):
        await plugin.wait_for_proxmox_task("UPID:pve:001:vzdump", timeout=5.0)


@pytest.mark.asyncio
async def test_wait_for_proxmox_task_raises_on_failure():
    plugin = _make_plugin()
    with patch.object(
        plugin,
        "get_proxmox_task_status",
        new=AsyncMock(return_value={"status": "stopped", "exitstatus": "FAIL"}),
    ):
        with pytest.raises(RuntimeError, match="failed"):
            await plugin.wait_for_proxmox_task("UPID:pve:001:vzdump", timeout=5.0)


@pytest.mark.asyncio
async def test_list_tasks_maps_active_rows():
    plugin = _make_plugin()
    plugin.ticket = "t"
    plugin.csrf_token = "c"
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "data": [
            {"type": "vzdump", "upid": "UPID:1", "id": 101, "user": "root@pam"},
            {"type": "qmstart", "upid": "UPID:2", "id": 101},
        ]
    }
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    with patch("app.plugins.proxmox.httpx.AsyncClient", return_value=fake_client):
        rows = await plugin.list_tasks(running_only=True, vmid=101, limit=10)
    assert len(rows) == 2
    assert rows[0]["type"] == "vzdump"
    assert rows[0]["vmid"] == 101
    assert rows[0]["node"] == "pve"
    _args, kwargs = fake_client.get.await_args
    assert kwargs["params"]["source"] == "active"
