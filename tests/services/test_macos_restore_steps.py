"""Steps of the macOS restore job: the target keeps its own identity and address, and its public link stays
down until the guest has been configured."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.plugins.base import PowerState
from app.services.deployment import macos_restore
from app.services.deployment.macos_restore import (
    CaptureMacosIdentityStep,
    ConfigureRestoredMacosGuestStep,
    ReapplyMacosIdentityStep,
    ReleasePublicLinkStep,
    WaitForRestoredGuestAgentStep,
    with_link_down,
    without_link_down,
)
from app.services.deployment.registry import get_deployment_strategy_registry
from app.services.deployment.step import DeploymentError, StepResult
from app.services.deployment.steps import _vm_restore_state, _write_vm_restore_state
from app.services.macos_smbios import smbios_from_smbios1

PUBLIC = "e1000-82545em=AA:BB:CC:00:00:02,bridge=vmbr0,tag=40"
PRIVATE = "e1000-82545em=AA:BB:CC:00:00:03,bridge=vmbr90,tag=3901"
SOURCE_PUBLIC = "e1000-82545em=AA:BB:CC:00:00:01,bridge=vmbr0"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _smbios(serial: str, uuid: str) -> str:
    return (
        f"uuid={uuid},base64=1,serial={_b64(serial)},manufacturer={_b64('Apple Inc.')},"
        f"product={_b64('iMacPro1,1')},family={_b64('Mac')}"
    )


TARGET_SMBIOS = _smbios("C02TARGET001", "11111111-2222-3333-4444-555555555555")
SOURCE_SMBIOS = _smbios("C02SOURCE001", "99999999-8888-7777-6666-555555555555")
TARGET_ARGS = "-device isa-applesmc,osk=target -cpu Haswell"
SOURCE_ARGS = "-device isa-applesmc,osk=source -cpu Haswell"


class FakeMac:
    """Proxmox plugin and macOS guest double."""

    def __init__(self, config=None):
        self.config = dict(config or {})
        self.set_calls = []
        self.exec_calls = []
        self.password_calls = []
        self.smbios1_updates = []
        self.agent_up = True
        self.agent_error = None
        self.power = PowerState.ON
        self.fail_network = False
        self.fail_password = None

    async def get_qemu_config(self, vmid=None):
        return dict(self.config)

    async def set_qemu_config(self, payload, *, delete=None, vmid=None):
        self.set_calls.append((dict(payload), list(delete or [])))
        self.config.update(payload)
        for key in delete or []:
            self.config.pop(key, None)

    async def update_smbios1(self, value):
        self.smbios1_updates.append(value)

    async def guest_agent_ready(self):
        if self.agent_error:
            raise RuntimeError(self.agent_error)
        return self.agent_up

    async def get_power_state(self):
        return self.power

    async def power_on(self):
        self.power = PowerState.ON
        return True

    async def guest_exec(self, argv, max_wait=None):
        self.exec_calls.append(list(argv))
        joined = " ".join(argv)
        if "listallnetworkservices" in joined:
            return {"exitcode": 0, "out-data": "An asterisk (*) denotes that a network service is disabled.\nEthernet\n"}
        if "networksetup -setmanual" in joined and self.fail_network:
            return {"exitcode": 1, "err-data": "boom"}
        if "diskutil" in joined and "list" in joined:
            return {"exitcode": 0, "out-data": "   1:  EFI OPENCORE  1.1 GB  disk1s1\n"}
        if "PlistBuddy" in joined:
            return {"exitcode": 0, "out-data": "PATCHED_OK"}
        if "config.plist" in joined:
            return {"exitcode": 0, "out-data": "/Volumes/OPENCORE/EFI/OC/config.plist\n"}
        return {"exitcode": 0, "out-data": ""}

    async def guest_set_user_password(self, username, password, old_password=None, old_passwords=None):
        if self.fail_password:
            raise RuntimeError(self.fail_password)
        self.password_calls.append((username, password, list(old_passwords or [])))


class Ctx:
    def __init__(self, plugin, *, state=None, alloc="default", source_service=None):
        self._plugin = plugin
        self._alloc = (
            SimpleNamespace(ip_address="192.0.2.7", gateway="192.0.2.1", subnet_mask="255.255.255.0", dns_servers=None)
            if alloc == "default"
            else alloc
        )
        self.db = object() if source_service is not None else None
        self._source = source_service
        self.job = SimpleNamespace(id=1)
        self.service = SimpleNamespace(
            name="mac-target",
            owner_user_id=7,
            config={
                "vm_plan": {"strategy_name": "macos_guest_agent"},
                "template_parameters": {"admin_password": "target-pw"},
                "vm_restore": {"volid": "pbs:backup/vm/5300/a", "start": True, **(state or {})},
            },
            vm=SimpleNamespace(guest_state=None, guest_last_error=None),
        )
        self.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)

    def get_plugin(self):
        return self._plugin

    def get_ip_allocation(self):
        return self._alloc

    def get_specs(self):
        return {}

    def require_placement(self):
        return 1, "pve", 5301


def _run(coro):
    return asyncio.run(coro)


def _set_state(ctx, **extra):
    _write_vm_restore_state(ctx, {**_vm_restore_state(ctx.service), **extra})


def _captured(public=PUBLIC, **config):
    return {
        "nets": {"net0": public},
        "name": "mac-target",
        "config": {"smbios1": TARGET_SMBIOS, "args": TARGET_ARGS, **config},
    }


# --- link helpers and strategy layout ---


def test_link_helpers_add_and_remove_only_the_link_option():
    down = with_link_down(PUBLIC)
    assert down.endswith(",link_down=1") and with_link_down(down).count("link_down") == 1
    assert without_link_down(down) == PUBLIC
    assert without_link_down(PUBLIC) == PUBLIC


def test_macos_restore_strategy_boots_configures_then_connects_before_finishing():
    names = get_deployment_strategy_registry().get("restore_retained_backup_macos").step_names()
    assert names.index("capture_target_identity") < names.index("start_retained_restore")
    assert names.index("wait_for_backup_restore") < names.index("reapply_target_network")
    assert (
        names.index("reapply_target_network")
        < names.index("power_on")
        < names.index("wait_for_restored_guest_agent")
        < names.index("configure_restored_macos_guest")
        < names.index("release_public_link")
        < names.index("finalize_backup_restore")
    )
    assert "reapply_cloudinit" not in names


def test_cloudinit_restore_strategy_is_unchanged():
    names = get_deployment_strategy_registry().get("restore_retained_backup").step_names()
    assert names == [
        "stop_guest_for_restore",
        "capture_target_identity",
        "start_retained_restore",
        "wait_for_backup_restore",
        "reapply_target_network",
        "configure_sizing",
        "reapply_cloudinit",
        "finalize_backup_restore",
    ]


def test_retry_strategy_skips_the_restore_and_reapplies_the_guest_configuration():
    names = get_deployment_strategy_registry().get("retry_restore_macos_configuration").step_names()
    assert names == [
        "power_on",
        "wait_for_restored_guest_agent",
        "configure_restored_macos_guest",
        "release_public_link",
        "finalize_backup_restore",
    ]


def test_macos_strategy_offers_staff_the_retry_action_only():
    strategy = get_deployment_strategy_registry().get("macos_guest_agent")
    action = strategy.action_by_name("retry_restore_configuration")
    assert action is not None and action.admin and not action.client_eligible


# --- capture ---


def test_capture_records_nics_name_and_macos_identity_settings():
    plugin = FakeMac(
        {"net0": PUBLIC, "net1": PRIVATE, "name": "mac-target", "smbios1": TARGET_SMBIOS, "args": TARGET_ARGS,
         "vmgenid": "abc", "memory": 4096}
    )
    ctx = Ctx(plugin)
    step = CaptureMacosIdentityStep()
    _run(step.execute(ctx))
    identity = _vm_restore_state(ctx.service)["target_identity"]
    assert identity["nets"] == {"net0": PUBLIC, "net1": PRIVATE}
    assert identity["config"] == {"smbios1": TARGET_SMBIOS, "args": TARGET_ARGS, "vmgenid": "abc"}
    assert _run(step.precheck(ctx)).result == StepResult.SKIP


def test_capture_ignores_a_link_left_down_by_an_earlier_failed_restore():
    plugin = FakeMac({"net0": with_link_down(PUBLIC), "name": "mac-target"})
    ctx = Ctx(plugin)
    _run(CaptureMacosIdentityStep().execute(ctx))
    assert _vm_restore_state(ctx.service)["target_identity"]["nets"]["net0"] == PUBLIC


# --- reapply ---


def test_reapply_restores_targets_identity_and_holds_the_public_link_down():
    restored = {
        "net0": SOURCE_PUBLIC,
        "net1": "e1000-82545em=AA:BB:CC:00:00:09,bridge=vmbr0",
        "smbios1": SOURCE_SMBIOS,
        "args": SOURCE_ARGS,
        "vmgenid": "from-source",
    }
    plugin = FakeMac(restored)
    ctx = Ctx(plugin, state={"target_identity": {**_captured(), "nets": {"net0": PUBLIC, "net1": PRIVATE}}})
    step = ReapplyMacosIdentityStep()
    assert _run(step.precheck(ctx)).result == StepResult.READY
    _run(step.execute(ctx))
    payload, deleted = plugin.set_calls[0]
    assert payload["net0"] == with_link_down(PUBLIC)
    assert payload["net1"] == PRIVATE and "link_down" not in payload["net1"]
    assert payload["smbios1"] == TARGET_SMBIOS and payload["args"] == TARGET_ARGS
    assert payload["name"] == "mac-target"
    assert deleted == ["vmgenid"]
    state = _vm_restore_state(ctx.service)
    assert state["link_held"] is True and state["identity_reapplied"] is True


def test_reapply_leaves_identity_settings_that_already_match_alone():
    plugin = FakeMac({"net0": SOURCE_PUBLIC, "smbios1": TARGET_SMBIOS, "args": TARGET_ARGS})
    ctx = Ctx(plugin, state={"target_identity": _captured()})
    _run(ReapplyMacosIdentityStep().execute(ctx))
    payload, deleted = plugin.set_calls[0]
    assert "smbios1" not in payload and "args" not in payload and deleted == []


def test_reapply_failure_is_reported_as_an_error():
    plugin = FakeMac({"net0": SOURCE_PUBLIC})

    async def refuse(payload, *, delete=None, vmid=None):
        raise RuntimeError("403 args need root")

    plugin.set_qemu_config = refuse
    ctx = Ctx(plugin, state={"target_identity": _captured()})
    with pytest.raises(DeploymentError, match="identity settings: 403 args need root"):
        _run(ReapplyMacosIdentityStep().execute(ctx))


def test_reapply_without_a_public_nic_holds_nothing():
    plugin = FakeMac({"net1": PRIVATE})
    ctx = Ctx(plugin, state={"target_identity": {"nets": {"net1": PRIVATE}, "name": "mac-target", "config": {}}})
    _run(ReapplyMacosIdentityStep().execute(ctx))
    assert _vm_restore_state(ctx.service)["link_held"] is False


# --- waiting for the guest agent ---


def test_wait_step_waits_then_passes_once_the_agent_answers():
    plugin = FakeMac()
    plugin.agent_up = False
    ctx = Ctx(plugin)
    step = WaitForRestoredGuestAgentStep()
    outcome = _run(step.precheck(ctx))
    assert outcome.result == StepResult.WAIT and "agent_wait_started" in _vm_restore_state(ctx.service)
    plugin.agent_up = True
    assert _run(step.precheck(ctx)).result == StepResult.READY
    assert "agent_wait_started" not in _vm_restore_state(ctx.service)


def test_wait_step_reports_a_readable_error_while_the_agent_is_unreachable():
    plugin = FakeMac()
    plugin.agent_error = "qga not running"
    outcome = _run(WaitForRestoredGuestAgentStep().precheck(Ctx(plugin)))
    assert outcome.result == StepResult.WAIT and outcome.detail == {"last_error": "qga not running"}


def test_guest_agent_that_never_comes_up_fails_the_job_and_errors_the_vm():
    plugin = FakeMac()
    plugin.agent_up = False
    started = datetime.now(timezone.utc) - timedelta(seconds=macos_restore.AGENT_WAIT_SECONDS + 5)
    ctx = Ctx(plugin, state={"agent_wait_started": started.isoformat(), "identity_reapplied": True})
    outcome = _run(WaitForRestoredGuestAgentStep().precheck(ctx))
    assert outcome.result == StepResult.ABORT
    assert "guest agent did not respond" in outcome.message
    assert "public network connection stays disconnected" in outcome.message
    assert "retry_restore_configuration" in outcome.message
    state = _vm_restore_state(ctx.service)
    assert state["status"] == "failed" and state["error"] == outcome.message
    assert ctx.service.vm.guest_state.value == "error" and ctx.service.vm.guest_last_error == outcome.message


# --- guest configuration ---


def _configure_ctx(plugin, **kwargs):
    state = {"target_identity": _captured(), "link_held": True, "identity_reapplied": True}
    return Ctx(plugin, state=state, **kwargs)


def test_configure_applies_the_targets_smbios_address_and_password():
    plugin = FakeMac({"net0": with_link_down(PUBLIC)})
    source_service = SimpleNamespace(
        owner_user_id=7, config={"template_parameters": {"admin_password": "source-pw"}}
    )
    ctx = _configure_ctx(plugin, source_service=source_service)
    _write_vm_restore_state(ctx, {**_vm_restore_state(ctx.service), "source_service_id": 4})
    rebooted = AsyncMock()
    with patch("app.dao.service_dao.ServiceDAO.get_by_id", return_value=source_service), patch(
        "app.services.deployment.guest_config.reboot_guest_and_wait_agent", rebooted
    ):
        step = ConfigureRestoredMacosGuestStep()
        assert _run(step.precheck(ctx)).result == StepResult.READY
        _run(step.execute(ctx))

    patch_script = next(" ".join(c) for c in plugin.exec_calls if any("PlistBuddy" in part for part in c))
    assert "C02TARGET001" in patch_script and "11111111-2222-3333-4444-555555555555" in patch_script
    assert "C02SOURCE001" not in patch_script
    assert plugin.smbios1_updates == []
    rebooted.assert_awaited_once()
    assert any("networksetup -setmanual" in " ".join(c) and "192.0.2.7" in " ".join(c) for c in plugin.exec_calls)
    assert any("192.0.2.1" in " ".join(c) for c in plugin.exec_calls)
    assert any("-setdnsservers" in " ".join(c) for c in plugin.exec_calls)
    username, password, olds = plugin.password_calls[0]
    assert (username, password) == ("client", "target-pw")
    assert "source-pw" in olds and "target-pw" not in olds
    state = _vm_restore_state(ctx.service)
    assert state["opencore_aligned"] is True and state["configure_attempts"] == 1
    assert "configure_error" not in state


def test_configure_does_not_try_another_clients_source_password():
    plugin = FakeMac()
    foreign = SimpleNamespace(owner_user_id=99, config={"template_parameters": {"admin_password": "other-pw"}})
    ctx = _configure_ctx(plugin, source_service=foreign)
    _write_vm_restore_state(ctx, {**_vm_restore_state(ctx.service), "source_service_id": 4})
    with patch("app.dao.service_dao.ServiceDAO.get_by_id", return_value=foreign), patch(
        "app.services.deployment.guest_config.reboot_guest_and_wait_agent", AsyncMock()
    ):
        _run(ConfigureRestoredMacosGuestStep().execute(ctx))
    assert "other-pw" not in plugin.password_calls[0][2]


def test_configure_skips_the_opencore_rewrite_when_it_was_already_done():
    plugin = FakeMac()
    ctx = _configure_ctx(plugin)
    _set_state(ctx, opencore_aligned=True)
    rebooted = AsyncMock()
    with patch("app.services.deployment.guest_config.reboot_guest_and_wait_agent", rebooted):
        _run(ConfigureRestoredMacosGuestStep().execute(ctx))
    assert not any("PlistBuddy" in " ".join(c) for c in plugin.exec_calls)
    rebooted.assert_not_awaited()
    assert any("networksetup -setmanual" in " ".join(c) for c in plugin.exec_calls)


def test_configure_uses_dhcp_when_the_template_says_so():
    plugin = FakeMac()
    ctx = _configure_ctx(plugin, alloc=None)
    ctx.service.config["vm_plan"]["strategy_plan"] = {"strategy_config": {"network_mode": "dhcp"}}
    _set_state(ctx, opencore_aligned=True)
    with patch("app.services.deployment.guest_config.reboot_guest_and_wait_agent", AsyncMock()):
        _run(ConfigureRestoredMacosGuestStep().execute(ctx))
    assert any("-setdhcp" in " ".join(c) for c in plugin.exec_calls)


def test_configure_failure_is_recorded_and_the_third_failure_fails_the_job():
    plugin = FakeMac()
    plugin.fail_network = True
    ctx = _configure_ctx(plugin)
    _set_state(ctx, opencore_aligned=True)
    step = ConfigureRestoredMacosGuestStep()
    for attempt in (1, 2, 3):
        assert _run(step.precheck(ctx)).result == StepResult.READY
        with pytest.raises(DeploymentError, match="setmanual failed"):
            _run(step.execute(ctx))
        assert _vm_restore_state(ctx.service)["configure_attempts"] == attempt
    outcome = _run(step.precheck(ctx))
    assert outcome.result == StepResult.ABORT
    assert "after 3 attempts" in outcome.message and "setmanual failed" in outcome.message
    assert "public network connection stays disconnected" in outcome.message
    state = _vm_restore_state(ctx.service)
    assert state["status"] == "failed"
    assert ctx.service.vm.guest_state.value == "error"
    assert state["link_held"] is True


def test_password_failure_stops_before_the_link_is_released():
    plugin = FakeMac()
    plugin.fail_password = "Secure Token rejected the password change"
    ctx = _configure_ctx(plugin)
    _set_state(ctx, opencore_aligned=True)
    with pytest.raises(RuntimeError, match="Secure Token"):
        _run(ConfigureRestoredMacosGuestStep().execute(ctx))
    assert _vm_restore_state(ctx.service)["link_held"] is True
    assert _vm_restore_state(ctx.service)["configure_error"].startswith("Secure Token")


def test_configure_without_a_target_serial_leaves_opencore_as_restored():
    plugin = FakeMac()
    ctx = Ctx(plugin, state={"target_identity": {"nets": {"net0": PUBLIC}, "config": {}}})
    with patch("app.services.deployment.guest_config.reboot_guest_and_wait_agent", AsyncMock()) as rebooted:
        _run(ConfigureRestoredMacosGuestStep().execute(ctx))
    assert not any("PlistBuddy" in " ".join(c) for c in plugin.exec_calls)
    rebooted.assert_not_awaited()


def test_smbios_is_rebuilt_from_the_targets_smbios1_deterministically():
    first = smbios_from_smbios1(TARGET_SMBIOS)
    assert first == smbios_from_smbios1(TARGET_SMBIOS)
    assert first["SystemSerialNumber"] == "C02TARGET001"
    assert first["SystemUUID"] == "11111111-2222-3333-4444-555555555555"
    assert first["SystemProductName"] == "iMacPro1,1"
    assert len(first["MLB"]) == 17 and len(first["ROM_HEX"]) == 12
    assert smbios_from_smbios1(SOURCE_SMBIOS)["MLB"] != first["MLB"]
    assert smbios_from_smbios1(None) is None and smbios_from_smbios1("uuid=1") is None


# --- connecting the public link ---


def test_release_connects_the_public_link_once_the_guest_is_configured():
    plugin = FakeMac({"net0": with_link_down(PUBLIC), "net1": PRIVATE})
    ctx = Ctx(plugin, state={"link_held": True})
    step = ReleasePublicLinkStep()
    assert _run(step.precheck(ctx)).result == StepResult.READY
    _run(step.execute(ctx))
    assert plugin.set_calls == [({"net0": PUBLIC}, [])]
    assert _vm_restore_state(ctx.service)["link_held"] is False


def test_release_is_skipped_when_no_link_was_held_and_repeats_safely():
    ctx = Ctx(FakeMac({"net0": PUBLIC}), state={"link_held": False})
    assert _run(ReleasePublicLinkStep().precheck(ctx)).result == StepResult.SKIP
    plugin = FakeMac({"net0": PUBLIC})
    ctx = Ctx(plugin, state={"link_held": True})
    _run(ReleasePublicLinkStep().execute(ctx))
    assert plugin.set_calls == []
