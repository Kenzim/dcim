import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models.service import ServiceType
from app.services.deployment.actions import CHANGE_PASSWORD_ACTION, RESET_NETWORK_ACTION
from app.services.deployment.guest_config import (
    apply_cloudinit_network_reset,
    build_cloudinit_network_payload,
)
from app.services.deployment.strategies import CloudinitCloneStrategy, MacosGuestAgentStrategy
from app.services.strategy_actions import list_actions, run_action
from app.services.vm_install_type_strategy import merge_strategy_options, resolve_vm_template_strategy


def test_macos_install_type_resolves():
    spec = resolve_vm_template_strategy("macOS - Guest agent")
    assert spec["strategy_name"] == "macos_guest_agent"
    assert "install-macos" in spec["billing_os_code"]


def test_merge_strategy_options_defaults():
    merged = merge_strategy_options("macOS - Guest agent", {"network_mode": "dhcp"})
    assert merged["network_mode"] == "dhcp"
    assert merged["guest_username"] == "client"
    assert merged["randomize_smbios"] is True


def test_macos_strategy_actions_include_smbios():
    s = MacosGuestAgentStrategy()
    names = {a.name for a in s.actions()}
    assert "change_password" in names
    assert "reset_network" in names
    assert "randomize_smbios" in names


def test_list_actions_admin_vs_client_allowlist(monkeypatch):
    strategy = MacosGuestAgentStrategy()
    monkeypatch.setattr(
        "app.services.strategy_actions.resolve_vm_strategy_name_for_service",
        lambda db, service: "macos_guest_agent",
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.get_deployment_strategy_registry",
        lambda: SimpleNamespace(resolve=lambda name: strategy),
    )
    monkeypatch.setattr(
        "app.services.strategy_actions._template_options",
        lambda db, service: {"client_actions": ["change_password"]},
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.resolve_client_permissions",
        lambda db, service: {
            "vm.change_password": True,
            "vm.reset_network": True,
        },
    )
    service = SimpleNamespace(service_type=ServiceType.VM, vm=None, config={})
    admin = list_actions(MagicMock(), service, "admin")
    client = list_actions(MagicMock(), service, "client")
    assert {a["name"] for a in admin} >= {"change_password", "reset_network", "randomize_smbios"}
    assert {a["name"] for a in client} == {"change_password"}
    assert CHANGE_PASSWORD_ACTION.name in {a["name"] for a in admin}
    assert RESET_NETWORK_ACTION.name in {a["name"] for a in admin}
    assert "cloud-init" in RESET_NETWORK_ACTION.description.lower()


def test_build_cloudinit_network_payload_static():
    alloc = SimpleNamespace(
        ip_address="192.0.2.10",
        gateway="192.0.2.1",
        subnet_mask="255.255.255.0",
    )
    payload = build_cloudinit_network_payload(
        alloc, ciuser="root", cipassword="s3cret"
    )
    assert payload["ipconfig0"] == "ip=192.0.2.10/24,gw=192.0.2.1"
    assert payload["ciuser"] == "root"
    assert payload["cipassword"] == "s3cret"
    assert payload["nameserver"] == "1.1.1.1 8.8.8.8"


def test_cloudinit_clone_defaults_guest_username_root():
    from app.services.vm_install_type_strategy import merge_strategy_options

    merged = merge_strategy_options("Linux - Cloudinit", {})
    assert merged["guest_username"] == "root"


def test_cloudinit_strategy_actions_include_defaults():
    names = {a.name for a in CloudinitCloneStrategy().actions()}
    assert "change_password" in names
    assert "reset_network" in names


def test_reset_network_cloudinit_clone_uses_cloudinit_path(monkeypatch):
    called = {}

    async def fake_apply(plugin, alloc, *, mode="static", nameserver=None):
        called["mode"] = mode
        called["alloc_ip"] = getattr(alloc, "ip_address", None)

    linux_calls = []

    async def fake_linux(*_a, **_k):
        linux_calls.append(True)

    plugin = MagicMock()
    alloc = SimpleNamespace(
        ip_address="192.0.2.10",
        gateway="192.0.2.1",
        subnet_mask="255.255.255.0",
    )

    monkeypatch.setattr(
        "app.services.strategy_actions.resolve_vm_strategy_name_for_service",
        lambda db, service: "cloudinit_clone",
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.get_deployment_strategy_registry",
        lambda: SimpleNamespace(resolve=lambda name: CloudinitCloneStrategy()),
    )
    monkeypatch.setattr(
        "app.services.strategy_actions._template_options",
        lambda db, service: {"client_actions": ["reset_network"], "network_mode": "static"},
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.list_actions",
        lambda db, service, audience: [{"name": "reset_network"}],
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.strategy_options_from_ctx",
        lambda ctx: {"network_mode": "static", "guest_username": "ubuntu"},
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.apply_cloudinit_network_reset",
        fake_apply,
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.configure_linux_network",
        fake_linux,
    )
    monkeypatch.setattr(
        "app.services.strategy_actions._plugin_for_service",
        lambda db, service: plugin,
    )
    monkeypatch.setattr(
        "app.dao.vm_ip_allocation_dao.VMIPAllocationDAO.get_by_id",
        lambda db, allocation_id: alloc,
    )

    service = SimpleNamespace(
        service_type=ServiceType.VM,
        vm=SimpleNamespace(vm_ip_allocation_id=1),
        config={"vm_plan": {"strategy_name": "cloudinit_clone"}},
    )

    result = asyncio.run(run_action(MagicMock(), service, "reset_network", {}, "admin"))
    assert result["via"] == "cloudinit"
    assert result["network_mode"] == "static"
    assert called["mode"] == "static"
    assert called["alloc_ip"] == "192.0.2.10"
    assert not linux_calls


def test_apply_cloudinit_network_reset_configure_clean_reboot():
    from app.plugins.base import PowerState

    class _Plugin:
        def __init__(self):
            self.vmid = 5000
            self.calls = []
            self._power = PowerState.ON
            self._agent = True

        async def configure_vm(self, vm_ref, vm_config):
            self.calls.append(("configure", vm_config))
            return True

        async def regenerate_cloudinit(self, vmid=None):
            self.calls.append(("regenerate_cloudinit", vmid))

        async def guest_exec(self, command, input_data=None, timeout=180.0):
            self.calls.append(("guest_exec", list(command)))
            return {"exitcode": 0, "out-data": "", "err-data": ""}

        async def guest_shutdown(self):
            self.calls.append("guest_shutdown")
            self._power = PowerState.OFF

        async def get_power_state(self):
            return self._power

        async def power_on(self):
            self.calls.append("power_on")
            self._power = PowerState.ON
            self._agent = True
            return True

        async def guest_agent_ready(self):
            return self._agent

    plugin = _Plugin()
    alloc = SimpleNamespace(
        ip_address="192.0.2.10",
        gateway="192.0.2.1",
        subnet_mask="255.255.255.0",
    )
    asyncio.run(apply_cloudinit_network_reset(plugin, alloc, mode="static"))
    kinds = [c[0] if isinstance(c, tuple) else c for c in plugin.calls]
    assert "configure" in kinds
    assert "regenerate_cloudinit" in kinds
    assert "guest_exec" in kinds
    assert "guest_shutdown" in kinds
    assert "power_on" in kinds
    configure_payload = next(c[1] for c in plugin.calls if isinstance(c, tuple) and c[0] == "configure")
    assert configure_payload["ipconfig0"] == "ip=192.0.2.10/24,gw=192.0.2.1"
    guest_exec_cmd = next(c[1] for c in plugin.calls if isinstance(c, tuple) and c[0] == "guest_exec")
    assert any("cloud-init clean" in part for part in guest_exec_cmd)
