"""Tests for DHCP regeneration triggered by server / boot task changes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import dhcp_sync


def _run(coro):
    return asyncio.run(coro)


def test_sync_pushes_location_config_to_runner():
    instance = SimpleNamespace(id=5)
    runner = AsyncMock(return_value=(200, {}))
    with patch.object(dhcp_sync.ServerDAO, "get_by_id", return_value=SimpleNamespace(location_id=3)), patch.object(
        dhcp_sync.ServiceInstanceDAO, "get_by_location_and_type", return_value=instance
    ), patch("app.services.dhcp_config_service.get_dhcp_config_service") as cfg_svc, patch(
        "app.services.dhcp_config_generator.generate_dhcpd_conf", return_value=("conf-body", ["eth0"])
    ) as gen, patch("app.services.runner_client.call_dhcp_runner", runner), patch.object(
        dhcp_sync, "_regenerate_global_dhcp", AsyncMock()
    ) as global_regen:
        cfg_svc.return_value.get_config_by_service_instance.return_value = MagicMock()
        _run(dhcp_sync.sync_dhcp_for_server(MagicMock(), 1))

    gen.assert_called_once()
    assert gen.call_args.kwargs["location_id"] == 3
    args, kwargs = runner.call_args_list[0]
    assert args[2:4] == ("PUT", "/config")
    assert kwargs["raw_body"] == b"conf-body"
    assert kwargs["extra_headers"] == {"X-Runner-Interfaces": "eth0"}
    global_regen.assert_not_called()


def _runner_responses(status_body):
    async def fake(instance, db, method, path, **kwargs):
        if (method, path) == ("GET", "/status"):
            return 200, status_body
        return 200, {"success": True}

    return AsyncMock(side_effect=fake)


def test_push_restarts_running_dhcpd_so_it_loads_the_new_config():
    runner = _runner_responses({"running": True})
    with patch("app.services.runner_client.call_dhcp_runner", runner):
        code, _ = _run(dhcp_sync.push_location_dhcp_config(MagicMock(), SimpleNamespace(id=5), "conf", ["eth0"]))

    assert code == 200
    calls = [c.args[2:4] for c in runner.call_args_list]
    assert calls == [("PUT", "/config"), ("GET", "/status"), ("POST", "/restart")]


def test_push_leaves_stopped_dhcpd_stopped():
    runner = _runner_responses({"running": False})
    with patch("app.services.runner_client.call_dhcp_runner", runner):
        _run(dhcp_sync.push_location_dhcp_config(MagicMock(), SimpleNamespace(id=5), "conf", None))

    calls = [c.args[2:4] for c in runner.call_args_list]
    assert ("POST", "/restart") not in calls


def test_push_does_not_restart_when_config_write_fails():
    runner = AsyncMock(return_value=(502, {"detail": "disk full"}))
    with patch("app.services.runner_client.call_dhcp_runner", runner):
        code, body = _run(dhcp_sync.push_location_dhcp_config(MagicMock(), SimpleNamespace(id=5), "conf", None))

    assert (code, body) == (502, {"detail": "disk full"})
    assert runner.await_count == 1


def test_sync_falls_back_to_global_config_without_location_instance():
    with patch.object(dhcp_sync.ServerDAO, "get_by_id", return_value=SimpleNamespace(location_id=3)), patch.object(
        dhcp_sync.ServiceInstanceDAO, "get_by_location_and_type", return_value=None
    ), patch.object(dhcp_sync, "_regenerate_global_dhcp", AsyncMock()) as global_regen:
        _run(dhcp_sync.sync_dhcp_for_server(MagicMock(), 1))
    global_regen.assert_awaited_once()


def test_sync_never_raises():
    with patch.object(dhcp_sync.ServerDAO, "get_by_id", side_effect=RuntimeError("db down")):
        _run(dhcp_sync.sync_dhcp_for_server(MagicMock(), 1))


@pytest.mark.parametrize("in_loop", [True, False])
def test_schedule_runs_sync_with_or_without_event_loop(monkeypatch, in_loop):
    real = dhcp_sync
    done = []

    async def fake_sync(server_id):
        done.append(server_id)

    monkeypatch.setattr(real, "_sync_in_own_session", fake_sync)

    if in_loop:
        async def caller():
            real.schedule_dhcp_sync(9)
            await asyncio.sleep(0.01)

        asyncio.run(caller())
    else:
        real.schedule_dhcp_sync(9)
        for _ in range(100):
            if done:
                break
            asyncio.run(asyncio.sleep(0.01))
    assert done == [9]
