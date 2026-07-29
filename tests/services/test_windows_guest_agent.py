"""Tests for the Windows guest-agent deployment strategy and helpers."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.deployment.guest_config import (
    configure_windows_network,
)
from app.services.deployment.registry import get_deployment_strategy_registry
from app.services.deployment.strategies import WindowsGuestAgentStrategy
from app.services.strategy_actions import run_action
from app.services.vm_install_type_strategy import merge_strategy_options
from app.services.vm_os_strategy import (
    VMProvisionRequest,
    get_vm_os_strategy_registry,
)


def test_windows_strategy_step_order():
    assert WindowsGuestAgentStrategy().step_names() == [
        "clone_from_template",
        "configure_sizing",
        "power_on",
        "wait_for_guest_agent",
        "configure_windows_guest",
        "stamp_vm_identity",
    ]


def test_registry_resolves_windows_guest_agent():
    reg = get_deployment_strategy_registry()
    strategy = reg.resolve("windows_guest_agent")
    assert strategy is not None
    assert strategy.name == "windows_guest_agent"


def test_windows_option_schema_defaults():
    merged = merge_strategy_options("Windows - Guest agent", None)
    assert merged["guest_username"] == "Administrator"
    assert merged["network_mode"] == "static"
    assert "change_password" in merged["client_actions"]


def test_windows_plan_builder():
    registry = get_vm_os_strategy_registry()
    strategy = registry.resolve("windows_guest_agent")
    plan = strategy.build_plan(
        VMProvisionRequest(
            service_id=1,
            product_code="win",
            os_code="install-windows-guest-agent",
            specs={},
            context={},
        ),
        strategy_config={"network_mode": "static"},
    )
    assert plan.strategy_name == "windows_guest_agent"
    assert plan.payload["mode"] == "windows_guest_agent"
    assert plan.payload["network"]["source"] == "vm_ip_allocation"


def test_configure_windows_network_static_calls_powershell():
    plugin = SimpleNamespace(guest_exec=AsyncMock(return_value={"exitcode": 0, "out-data": "NET_OK", "err-data": ""}))
    alloc = SimpleNamespace(
        ip_address="10.0.0.50",
        subnet_mask="255.255.255.0",
        gateway="10.0.0.1",
        dns_servers="1.1.1.1 8.8.8.8",
    )
    asyncio.run(configure_windows_network(plugin, mode="static", alloc=alloc))
    plugin.guest_exec.assert_awaited_once()
    argv = plugin.guest_exec.await_args.args[0]
    assert argv[0] == "powershell.exe"
    script = argv[-1]
    assert "New-NetIPAddress" in script
    assert "10.0.0.50" in script
    assert "10.0.0.1" in script
    assert "PrefixLength 24" in script


def test_configure_windows_network_dhcp():
    plugin = SimpleNamespace(guest_exec=AsyncMock(return_value={"exitcode": 0, "out-data": "NET_OK", "err-data": ""}))
    asyncio.run(configure_windows_network(plugin, mode="dhcp", alloc=None))
    script = plugin.guest_exec.await_args.args[0][-1]
    assert "Dhcp Enabled" in script


def test_configure_windows_network_static_requires_alloc():
    plugin = SimpleNamespace(guest_exec=AsyncMock())
    with pytest.raises(Exception, match="VM IP allocation"):
        asyncio.run(configure_windows_network(plugin, mode="static", alloc=None))


def test_reset_network_windows_branch(monkeypatch):
    from app.models.service import ServiceType

    called = {}

    async def fake_windows(plugin, *, mode="static", alloc=None, dns=None):
        called["mode"] = mode
        called["alloc_ip"] = getattr(alloc, "ip_address", None)
        called["plugin"] = plugin

    linux_calls = []

    async def fake_linux(*_a, **_k):
        linux_calls.append(True)

    monkeypatch.setattr(
        "app.services.strategy_actions.resolve_vm_strategy_name_for_service",
        lambda db, service: "windows_guest_agent",
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.get_deployment_strategy_registry",
        lambda: SimpleNamespace(resolve=lambda name: WindowsGuestAgentStrategy()),
    )
    monkeypatch.setattr(
        "app.services.strategy_actions._template_options",
        lambda db, service: {
            "client_actions": ["reset_network"],
            "network_mode": "static",
            "guest_username": "Administrator",
        },
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.list_actions",
        lambda db, service, audience: [{"name": "reset_network"}],
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.strategy_options_from_ctx",
        lambda ctx: {"network_mode": "static", "guest_username": "Administrator"},
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.configure_windows_network",
        fake_windows,
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.configure_linux_network",
        fake_linux,
    )

    plugin = MagicMock()
    alloc = SimpleNamespace(ip_address="10.0.0.9", subnet_mask="255.255.255.0", gateway="10.0.0.1")
    async def _fake_plugin_for_service(db, service):
        return plugin

    monkeypatch.setattr(
        "app.services.strategy_actions._plugin_for_service",
        _fake_plugin_for_service,
    )

    service = SimpleNamespace(
        service_type=ServiceType.VM,
        vm=SimpleNamespace(vm_ip_allocation_id=1),
        config={"vm_plan": {"strategy_name": "windows_guest_agent"}},
    )

    from app.services import strategy_actions as sa

    orig_init = sa._ActionCtx.__init__

    async def _fake_get_plugin():
        return plugin

    def _init(self, db, service):
        orig_init(self, db, service)
        self.get_ip_allocation = lambda: alloc
        self.get_plugin = _fake_get_plugin

    monkeypatch.setattr(sa._ActionCtx, "__init__", _init)

    result = asyncio.run(run_action(MagicMock(), service, "reset_network", {}, "admin"))
    assert result["status"] == "ok"
    assert result["network_mode"] == "static"
    assert called["mode"] == "static"
    assert called["alloc_ip"] == "10.0.0.9"
    assert not linux_calls
