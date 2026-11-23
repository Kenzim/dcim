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
        "apply_firewall",
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


def test_configure_refuses_to_leave_the_source_identity_when_no_target_identity_was_recorded():
    plugin = FakeMac()
    ctx = Ctx(plugin, state={"target_identity": {"nets": {"net0": PUBLIC}, "config": {}}})
    with patch("app.services.deployment.guest_config.reboot_guest_and_wait_agent", AsyncMock()) as rebooted:
        with pytest.raises(DeploymentError, match="SMBIOS identity was not recorded"):
            _run(ConfigureRestoredMacosGuestStep().execute(ctx))
    assert not any("PlistBuddy" in " ".join(c) for c in plugin.exec_calls)
    rebooted.assert_not_awaited()


def test_target_without_an_smbios_serial_gets_a_fresh_identity_in_proxmox_and_opencore():
    plugin = FakeMac({"net0": PUBLIC, "name": "mac-target", "smbios1": "uuid=11111111-2222-3333-4444-555555555555"})
    ctx = Ctx(plugin)
    _run(CaptureMacosIdentityStep().execute(ctx))
    identity = _vm_restore_state(ctx.service)["target_identity"]
    generated = identity["config"]["smbios1"]
    assert identity["smbios_generated"] is True
    fresh = smbios_from_smbios1(generated)
    assert fresh is not None and fresh["SystemSerialNumber"] != "C02SOURCE001"

    # The restore brings the source's identity; the target's fresh one replaces it in Proxmox ...
    plugin.config.update({"net0": SOURCE_PUBLIC, "smbios1": SOURCE_SMBIOS})
    _run(ReapplyMacosIdentityStep().execute(ctx))
    payload, deleted = plugin.set_calls[0]
    assert payload["smbios1"] == generated and "smbios1" not in deleted
    assert plugin.config["smbios1"] == generated

    # ... and in OpenCore on the disk.
    with patch("app.services.deployment.guest_config.reboot_guest_and_wait_agent", AsyncMock()):
        _run(ConfigureRestoredMacosGuestStep().execute(ctx))
    script = next(" ".join(c) for c in plugin.exec_calls if any("PlistBuddy" in part for part in c))
    assert fresh["SystemSerialNumber"] in script and fresh["SystemUUID"] in script
    assert "C02SOURCE001" not in script and "99999999-8888" not in script


def test_target_without_any_smbios1_also_gets_a_fresh_identity():
    plugin = FakeMac({"net0": PUBLIC})
    ctx = Ctx(plugin)
    _run(CaptureMacosIdentityStep().execute(ctx))
    identity = _vm_restore_state(ctx.service)["target_identity"]
    assert smbios_from_smbios1(identity["config"]["smbios1"]) is not None


def test_target_with_its_own_smbios_identity_is_not_given_a_new_one():
    plugin = FakeMac({"net0": PUBLIC, "smbios1": TARGET_SMBIOS})
    ctx = Ctx(plugin)
    _run(CaptureMacosIdentityStep().execute(ctx))
    identity = _vm_restore_state(ctx.service)["target_identity"]
    assert identity["config"]["smbios1"] == TARGET_SMBIOS and "smbios_generated" not in identity


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


# --- however the job fails, the restore is recorded as failed ---


@pytest.mark.parametrize("name", ["restore_retained_backup_macos", "retry_restore_macos_configuration"])
def test_a_failed_macos_restore_job_always_records_a_failed_restore(name):
    ctx = Ctx(FakeMac(), state={"status": "restored", "identity_reapplied": True, "link_held": True})
    job = SimpleNamespace(service=ctx.service, id=3, strategy_name=name)
    get_deployment_strategy_registry().get(name).on_job_failed(None, job, "Exceeded max reclaim attempts (2)")
    state = _vm_restore_state(ctx.service)
    assert state["status"] == "failed" and state["error"] == "Exceeded max reclaim attempts (2)"
    assert state["link_held"] is True and state["identity_reapplied"] is True
    assert ctx.service.vm.guest_state.value == "error"
    assert ctx.service.vm.guest_last_error == "Exceeded max reclaim attempts (2)"
    assert ctx.service.config["vm_plan"] == {"strategy_name": "macos_guest_agent"}


def test_a_failure_hook_keeps_the_more_specific_error_a_step_already_recorded():
    ctx = Ctx(FakeMac(), state={"status": "failed", "error": "The macOS guest agent did not respond"})
    macos_restore.record_restore_failure(ctx.service, "step 'wait_for_restored_guest_agent' failed")
    assert _vm_restore_state(ctx.service)["error"] == "The macOS guest agent did not respond"


def test_a_failure_hook_never_downgrades_a_finished_restore():
    ctx = Ctx(FakeMac(), state={"status": "success"})
    macos_restore.record_restore_failure(ctx.service, "late failure")
    assert _vm_restore_state(ctx.service)["status"] == "success"
    assert ctx.service.vm.guest_state is None


def test_other_strategies_have_no_failure_hook_side_effects():
    ctx = Ctx(FakeMac(), state={"status": "running"})
    job = SimpleNamespace(service=ctx.service, id=3)
    get_deployment_strategy_registry().get("restore_retained_backup").on_job_failed(None, job, "boom")
    assert _vm_restore_state(ctx.service)["status"] == "running" and ctx.service.vm.guest_state is None


def test_failure_to_connect_the_link_leaves_it_held_and_the_job_failure_is_recorded():
    plugin = FakeMac({"net0": with_link_down(PUBLIC)})

    async def refuse(payload, *, delete=None, vmid=None):
        raise RuntimeError("proxmox unreachable")

    plugin.set_qemu_config = refuse
    ctx = Ctx(plugin, state={"link_held": True, "identity_reapplied": True, "status": "restored"})
    with pytest.raises(RuntimeError, match="proxmox unreachable"):
        _run(ReleasePublicLinkStep().execute(ctx))
    assert _vm_restore_state(ctx.service)["link_held"] is True
    macos_restore.record_restore_failure(ctx.service, "proxmox unreachable")
    assert _vm_restore_state(ctx.service)["status"] == "failed"
    assert "link_down" in plugin.config["net0"]


# --- the window between the restore and the target's identity being put back ---


def test_nothing_before_the_identity_is_put_back_can_start_the_vm():
    steps = get_deployment_strategy_registry().get("restore_retained_backup_macos").steps()
    names = [s.name for s in steps]
    before = names[: names.index("reapply_target_network")]
    assert before == ["stop_guest_for_restore", "capture_target_identity", "start_retained_restore", "wait_for_backup_restore"]
    assert not any(type(s).__name__ in ("PowerOnStep", "FinalizeBackupRestoreStep") for s in steps[: len(before)])


def test_the_restore_task_is_started_without_booting_the_vm_whatever_start_says():
    from app.services.deployment.steps import StartRetainedRestoreStep

    plugin = FakeMac({"net0": PUBLIC})
    plugin.power = PowerState.OFF
    calls = []

    async def restore_backup(**kwargs):
        calls.append(kwargs)
        return "UPID:1"

    plugin.restore_backup = restore_backup
    ctx = Ctx(plugin, state={"start": True})
    _run(StartRetainedRestoreStep().execute(ctx))
    assert calls[0]["start"] is False and calls[0]["force"] is True
    assert plugin.power == PowerState.OFF
    assert ctx.service.vm.guest_state.value == "provisioning"


@pytest.mark.parametrize(
    ("state", "pending"),
    [
        ({"macos": True, "upid": "UPID:1", "status": "running"}, True),
        ({"macos": True, "upid": "UPID:1", "status": "failed", "error": "x"}, True),
        ({"macos": True, "carried_identity": True, "status": "queued"}, True),
        ({"macos": True, "status": "queued"}, False),
        ({"macos": True, "upid": "UPID:1", "identity_reapplied": True, "status": "failed"}, False),
        ({"macos": True, "upid": "UPID:1", "status": "success"}, False),
        ({"upid": "UPID:1", "status": "running"}, False),
        ({}, False),
    ],
)
def test_identity_is_pending_from_the_restore_until_it_is_put_back(state, pending):
    ctx = Ctx(FakeMac(), state=state)
    assert macos_restore.identity_restore_pending(ctx.service) is pending
