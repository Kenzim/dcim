"""Unit tests for restore, clone, password, and identity deployment steps."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.plugins.base import PowerState
from app.services.deployment.step import DeploymentError, StepResult
from app.services.deployment.steps import (
    ApplyGuestPasswordStep,
    CloneFromTemplateStep,
    ConfigureCloudInitNetworkStep,
    ConfigureSizingStep,
    FinalizeBackupRestoreStep,
    PowerOnStep,
    StampVmIdentityStep,
    StartBackupRestoreStep,
    StopGuestForRestoreStep,
    WaitForBackupRestoreStep,
    WaitForGuestAgentStep,
    _set_guest_state_after_restore,
    _vm_restore_state,
    _write_vm_restore_state,
)
from app.models.service_vm import VMGuestState


class FakePlugin:
    def __init__(self, *, exists=True, power=PowerState.ON):
        self._exists = exists
        self._power = power
        self.calls = []
        self.task_status = {"status": "stopped", "exitstatus": "OK"}
        self.task_error = None
        self.exists_error = None
        self.power_error = None
        self.configure_ok = True
        self.power_on_ok = True

    async def vm_exists(self):
        if self.exists_error:
            raise self.exists_error
        return self._exists

    async def get_power_state(self):
        if self.power_error:
            raise self.power_error
        return self._power

    async def clone_vm_from_template(self, template_ref, vm_config):
        self.calls.append(("clone", dict(vm_config), dict(template_ref)))
        return {"vmid": vm_config.get("vmid"), "task": "UPID:clone"}

    async def wait_for_proxmox_task(self, upid):
        self.calls.append(("wait", upid))

    async def configure_vm(self, vm_ref, vm_config):
        self.calls.append(("configure", vm_config))
        return self.configure_ok

    async def get_proxmox_task_status(self, upid):
        if self.task_error:
            raise self.task_error
        return dict(self.task_status)

    async def power_on(self):
        self.calls.append("power_on")
        if not self.power_on_ok:
            return False
        self._power = PowerState.ON
        return True

    async def guest_agent_ready(self):
        return True


class RestoreCtx:
    def __init__(self, plugin, *, config=None, db=None):
        self._plugin = plugin
        self.db = db
        self.service = SimpleNamespace(
            name="restore-vm",
            config=config if config is not None else {},
            vm=SimpleNamespace(guest_state=None, guest_last_error="old"),
        )
        self.logger = SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        )
        self._specs = {"memory_mb": 2048, "cores": 2, "full_clone": True}
        self._alloc = None
        self.template_node = "pve1"
        self.placed = (1, "pve1", 5000)

    def get_plugin(self):
        return self._plugin

    def get_ip_allocation(self):
        return self._alloc

    def get_specs(self):
        return dict(self._specs)

    def require_placement(self):
        return self.placed

    async def resolve_template_location(self):
        return (self.template_node, 9000)


def _run(coro):
    return asyncio.run(coro)


def test_vm_restore_state_helpers():
    assert _vm_restore_state(SimpleNamespace(config=None)) == {}
    assert _vm_restore_state(SimpleNamespace(config={"vm_restore": "x"})) == {}
    ctx = RestoreCtx(FakePlugin(), config={})
    _write_vm_restore_state(ctx, {"volid": "pbs:backup"})
    assert ctx.service.config["vm_restore"]["volid"] == "pbs:backup"


def test_stop_guest_for_restore_precheck_and_execute():
    plugin = FakePlugin(exists=False)
    ctx = RestoreCtx(plugin)
    out = _run(StopGuestForRestoreStep().precheck(ctx))
    assert out.result == StepResult.SKIP

    plugin.exists_error = RuntimeError("bmc down")
    out = _run(StopGuestForRestoreStep().precheck(ctx))
    assert out.result == StepResult.SKIP

    plugin.exists_error = None
    plugin._exists = True
    plugin.power_error = RuntimeError("status")
    out = _run(StopGuestForRestoreStep().precheck(ctx))
    assert out.result == StepResult.WAIT

    plugin.power_error = None
    plugin._power = PowerState.OFF
    out = _run(StopGuestForRestoreStep().precheck(ctx))
    assert out.result == StepResult.SKIP

    plugin._power = PowerState.ON
    out = _run(StopGuestForRestoreStep().precheck(ctx))
    assert out.result == StepResult.READY

    with patch(
        "app.services.vm_backup_service.stop_guest_for_restore",
        new=AsyncMock(return_value=None),
    ) as stop:
        _run(StopGuestForRestoreStep().execute(ctx))
        assert stop.await_count == 1


def test_start_and_wait_backup_restore():
    plugin = FakePlugin()
    ctx = RestoreCtx(plugin, config={"vm_restore": {"upid": "UPID:1"}})
    assert _run(StartBackupRestoreStep().precheck(ctx)).result == StepResult.SKIP

    ctx.service.config = {"vm_restore": {}}
    assert _run(StartBackupRestoreStep().precheck(ctx)).result == StepResult.FAILED

    ctx.service.config = {"vm_restore": {"volid": "pbs:vm/100"}}
    assert _run(StartBackupRestoreStep().precheck(ctx)).result == StepResult.READY

    with patch(
        "app.services.vm_backup_service.start_restore_task",
        new=AsyncMock(
            return_value={"upid": "UPID:r", "kind": "pbs", "volid": "pbs:vm/100", "storage": "pbs"}
        ),
    ):
        _run(StartBackupRestoreStep().execute(ctx))
    assert ctx.service.config["vm_restore"]["upid"] == "UPID:r"
    assert ctx.service.vm.guest_state == VMGuestState.PROVISIONING

    ctx.service.config = {"vm_restore": {}}
    assert _run(WaitForBackupRestoreStep().precheck(ctx)).result == StepResult.FAILED

    ctx.service.config = {"vm_restore": {"upid": "UPID:r"}}
    plugin.task_error = RuntimeError("not yet")
    assert _run(WaitForBackupRestoreStep().precheck(ctx)).result == StepResult.WAIT

    plugin.task_error = None
    plugin.task_status = {"status": "running"}
    assert _run(WaitForBackupRestoreStep().precheck(ctx)).result == StepResult.WAIT

    plugin.task_status = {"status": "stopped", "exitstatus": "ERROR"}
    assert _run(WaitForBackupRestoreStep().precheck(ctx)).result == StepResult.FAILED

    plugin.task_status = {"status": "stopped", "exitstatus": "OK"}
    assert _run(WaitForBackupRestoreStep().precheck(ctx)).result == StepResult.READY
    _run(WaitForBackupRestoreStep().execute(ctx))
    assert ctx.service.config["vm_restore"]["status"] == "restored"


def test_set_guest_state_after_restore_and_finalize():
    plugin = FakePlugin()
    ctx = RestoreCtx(plugin, config={"vm_restore": {"start": True, "volid": "pbs:vm/1"}})
    _run(_set_guest_state_after_restore(ctx, plugin, True))
    assert ctx.service.vm.guest_state == VMGuestState.RUNNING

    plugin.power_on_ok = False
    with pytest.raises(DeploymentError, match="power_on returned failure"):
        _run(_set_guest_state_after_restore(ctx, plugin, True))

    async def boom():
        raise RuntimeError("no power")

    plugin.power_on = boom  # type: ignore[method-assign]
    with pytest.raises(DeploymentError, match="Failed to start"):
        _run(_set_guest_state_after_restore(ctx, plugin, True))

    plugin = FakePlugin()
    ctx = RestoreCtx(plugin)
    _run(_set_guest_state_after_restore(ctx, plugin, False))
    assert ctx.service.vm.guest_state == VMGuestState.STOPPED

    ctx = RestoreCtx(plugin, config={"vm_restore": {"volid": "pbs:vm/1", "start": False}})
    assert _run(FinalizeBackupRestoreStep().precheck(ctx)).result == StepResult.READY
    with patch(
        "app.services.vm_backup_service.apply_metadata_after_restore"
    ) as apply_meta, patch(
        "app.services.vm_identity_stamp.resolve_template_id_for_restore",
        new=AsyncMock(return_value=9),
    ), patch(
        "app.services.vm_identity_stamp.stamp_vm_identity",
        new=AsyncMock(side_effect=RuntimeError("stamp failed")),
    ), patch(
        "app.services.vm_backup_service.lookup_backup_notes",
        new=AsyncMock(return_value="rf1 notes"),
    ):
        _run(FinalizeBackupRestoreStep().execute(ctx))
        apply_meta.assert_called_once()
    assert ctx.service.config["vm_restore"]["status"] == "success"
    assert ctx.service.vm.guest_last_error is None


def test_clone_sizing_password_and_stamp_steps():
    plugin = FakePlugin()
    ctx = RestoreCtx(plugin)
    plugin.exists_error = RuntimeError("lookup")
    assert _run(CloneFromTemplateStep().precheck(ctx)).result == StepResult.READY
    plugin.exists_error = None
    plugin._exists = True
    assert _run(CloneFromTemplateStep().precheck(ctx)).result == StepResult.SKIP

    plugin._exists = False
    ctx.template_node = "pve-other"
    ctx.placed = (1, "pve1", 5000)
    _run(CloneFromTemplateStep().execute(ctx))
    assert any(c[0] == "clone" and "target_node" in c[1] for c in plugin.calls)
    assert any(c[0] == "wait" for c in plugin.calls)

    plugin.configure_ok = False
    with pytest.raises(DeploymentError, match="sizing config"):
        _run(ConfigureSizingStep().execute(ctx))

    plugin.configure_ok = True
    ctx._specs = {"memory_mb": 1024, "cores": 1, "disk_gb": "nope"}
    with pytest.raises(DeploymentError, match="Invalid disk_gb"):
        _run(ConfigureSizingStep().execute(ctx))

    ctx._specs = {"memory_mb": 1024, "cores": 1, "disk_gb": 0}
    _run(ConfigureSizingStep().execute(ctx))

    ctx._specs = {"memory_mb": 1024, "cores": 1, "disk_gb": 50}
    _run(ConfigureSizingStep().execute(ctx))

    ctx.service.config = {}
    out = _run(ApplyGuestPasswordStep().precheck(ctx))
    assert out.result == StepResult.READY
    with pytest.raises(DeploymentError, match="No desired guest password"):
        _run(ApplyGuestPasswordStep().execute(ctx))
    ctx.service.config = {
        "template_parameters": {
            "admin_username": "root",
            "admin_password": "newpass",
            "previous_admin_password": "oldpass",
        }
    }
    with patch(
        "app.services.deployment.guest_config.set_guest_password",
        new=AsyncMock(return_value=None),
    ) as set_pw:
        _run(ApplyGuestPasswordStep().execute(ctx))
        assert set_pw.await_count == 1

    with patch(
        "app.services.vm_identity_stamp.stamp_vm_identity",
        new=AsyncMock(return_value="tok"),
    ):
        _run(StampVmIdentityStep().execute(ctx))
        assert _run(StampVmIdentityStep().precheck(ctx)).result == StepResult.READY


def test_power_on_wait_agent_and_cloudinit_prechecks():
    plugin = FakePlugin(power=PowerState.ON)
    ctx = RestoreCtx(plugin)
    assert _run(PowerOnStep().precheck(ctx)).result == StepResult.SKIP

    plugin.power_error = RuntimeError("unknown")
    plugin._power = PowerState.OFF
    # get_power_state error maps to UNKNOWN, not ON, so ready
    assert _run(PowerOnStep().precheck(ctx)).result == StepResult.READY
    plugin.power_error = None

    async def fail_power():
        raise RuntimeError("no juice")

    plugin.power_on = fail_power  # type: ignore[method-assign]
    with pytest.raises(DeploymentError, match="power_on failed"):
        _run(PowerOnStep().execute(ctx))

    plugin = FakePlugin(power=PowerState.OFF)
    plugin.power_on_ok = False
    ctx = RestoreCtx(plugin)
    with pytest.raises(DeploymentError, match="returned failure"):
        _run(PowerOnStep().execute(ctx))

    plugin.power_on_ok = True

    async def lie_then_off():
        plugin.calls.append("power_on")
        return True

    async def still_off():
        return PowerState.OFF

    plugin.power_on = lie_then_off  # type: ignore[method-assign]
    plugin.get_power_state = still_off  # type: ignore[method-assign]
    with pytest.raises(DeploymentError, match="not running"):
        _run(PowerOnStep().execute(ctx))

    async def ok_power():
        return True

    async def boom_state():
        raise RuntimeError("status lost")

    plugin.power_on = ok_power  # type: ignore[method-assign]
    plugin.get_power_state = boom_state  # type: ignore[method-assign]
    with pytest.raises(DeploymentError, match="Could not verify"):
        _run(PowerOnStep().execute(ctx))

    plugin = FakePlugin()
    ctx = RestoreCtx(plugin)

    async def agent_down():
        raise RuntimeError("no agent")

    plugin.guest_agent_ready = agent_down  # type: ignore[method-assign]
    assert _run(WaitForGuestAgentStep().precheck(ctx)).result == StepResult.WAIT

    async def not_ready():
        return False

    plugin.guest_agent_ready = not_ready  # type: ignore[method-assign]
    assert _run(WaitForGuestAgentStep().precheck(ctx)).result == StepResult.WAIT

    async def ready():
        return True

    plugin.guest_agent_ready = ready  # type: ignore[method-assign]
    assert _run(WaitForGuestAgentStep().precheck(ctx)).result == StepResult.READY

    ctx._alloc = None
    assert _run(ConfigureCloudInitNetworkStep().precheck(ctx)).result == StepResult.FAILED
    ctx._alloc = SimpleNamespace(ip_address="", gateway="", subnet_mask="")
    assert _run(ConfigureCloudInitNetworkStep().precheck(ctx)).result == StepResult.FAILED
    ctx._alloc = SimpleNamespace(ip_address="10.0.0.2", gateway="10.0.0.1", subnet_mask="255.255.255.0")
    assert _run(ConfigureCloudInitNetworkStep().precheck(ctx)).result == StepResult.READY

    plugin = FakePlugin()
    plugin.configure_ok = True
    ctx = RestoreCtx(plugin)
    ctx._specs = {"memory_mb": 1024, "cores": 1, "network_bridge": "vmbr9"}
    # FakePlugin has ensure_network_bridge only if we add it; default class does not.
    _run(ConfigureSizingStep().execute(ctx))


def test_guest_configure_precheck_waits():
    from app.services.deployment.steps import (
        ConfigureMacosGuestStep,
        ConfigureViaGuestAgentStep,
        ConfigureWindowsGuestStep,
    )

    plugin = FakePlugin()
    ctx = RestoreCtx(plugin)

    async def boom():
        raise RuntimeError("down")

    async def no():
        return False

    async def yes():
        return True

    for step_cls in (ConfigureViaGuestAgentStep, ConfigureMacosGuestStep, ConfigureWindowsGuestStep):
        plugin.guest_agent_ready = boom  # type: ignore[method-assign]
        assert _run(step_cls().precheck(ctx)).result == StepResult.WAIT
        plugin.guest_agent_ready = no  # type: ignore[method-assign]
        assert _run(step_cls().precheck(ctx)).result == StepResult.WAIT
        plugin.guest_agent_ready = yes  # type: ignore[method-assign]
        assert _run(step_cls().precheck(ctx)).result == StepResult.READY

    plugin.guest_agent_ready = boom  # type: ignore[method-assign]
    assert _run(ApplyGuestPasswordStep().precheck(ctx)).result == StepResult.WAIT
    plugin.guest_agent_ready = no  # type: ignore[method-assign]
    assert _run(ApplyGuestPasswordStep().precheck(ctx)).result == StepResult.WAIT

