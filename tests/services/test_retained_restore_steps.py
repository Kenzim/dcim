"""Steps of the retained-backup restore job: the target's identity is captured before and re-applied after."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.deployment.registry import get_deployment_strategy_registry
from app.services.deployment.step import DeploymentError, StepResult
from app.services.deployment.steps import (
    CaptureTargetIdentityStep,
    ReapplyCloudInitStep,
    ReapplyTargetNetworkStep,
    StartRetainedRestoreStep,
    _vm_restore_state,
    _write_vm_restore_state,
)

TARGET_NET = "virtio=AA:BB:CC:00:00:02,bridge=vmbr1,tag=40"


class FakeVm:
    def __init__(self, config):
        self.config = config
        self.restore_calls = []
        self.set_calls = []
        self.configure_calls = []

    async def get_qemu_config(self, vmid=None):
        return dict(self.config)

    async def restore_backup(self, **kwargs):
        self.restore_calls.append(kwargs)
        return "UPID:restore"

    async def set_qemu_config(self, payload, *, delete=None, vmid=None):
        self.set_calls.append((dict(payload), list(delete or [])))

    async def configure_vm(self, ref, payload):
        self.configure_calls.append((ref, dict(payload)))
        return True


class Ctx:
    def __init__(self, plugin, alloc=None):
        self._plugin = plugin
        self._alloc = alloc
        self.db = None
        self.service = SimpleNamespace(
            name="target-vm",
            config={"vm_restore": {"volid": "pbs-client:backup/vm/5100/a", "start": True}},
            vm=SimpleNamespace(guest_state=None, guest_last_error=None),
        )
        self.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)

    def get_plugin(self):
        return self._plugin

    def get_ip_allocation(self):
        return self._alloc

    def get_specs(self):
        return {"memory_mb": 4096, "cores": 4}

    def require_placement(self):
        return 1, "pve", 5101


def _run(coro):
    return asyncio.run(coro)


def test_strategy_is_registered_with_identity_steps_around_the_restore():
    names = get_deployment_strategy_registry().get("restore_retained_backup").step_names()
    assert names.index("capture_target_identity") < names.index("start_retained_restore")
    assert names.index("wait_for_backup_restore") < names.index("reapply_target_network")
    assert names.index("reapply_target_network") < names.index("configure_sizing")
    assert names.index("reapply_cloudinit") < names.index("finalize_backup_restore")


def test_capture_records_the_targets_nics_and_name_once():
    plugin = FakeVm({"net0": TARGET_NET, "name": "target-vm", "memory": 1024})
    ctx = Ctx(plugin)
    step = CaptureTargetIdentityStep()
    assert _run(step.precheck(ctx)).result == StepResult.READY
    _run(step.execute(ctx))
    assert _vm_restore_state(ctx.service)["target_identity"] == {"nets": {"net0": TARGET_NET}, "name": "target-vm"}
    assert _run(step.precheck(ctx)).result == StepResult.SKIP


def test_start_restores_into_the_target_vmid_without_powering_on():
    plugin = FakeVm({})
    ctx = Ctx(plugin)
    step = StartRetainedRestoreStep()
    _run(step.execute(ctx))
    call = plugin.restore_calls[0]
    assert call["archive"] == "pbs-client:backup/vm/5100/a" and call["vmid"] == 5101
    assert call["force"] is True and call["start"] is False
    assert _vm_restore_state(ctx.service)["upid"] == "UPID:restore"
    assert _run(step.precheck(ctx)).result == StepResult.SKIP


def test_reapply_network_puts_back_target_macs_and_drops_the_sources():
    # The restore brought the source VM's two interfaces; the target only has net0.
    plugin = FakeVm({"net0": "virtio=AA:BB:CC:00:00:01,bridge=vmbr0", "net1": "virtio=AA:BB:CC:00:00:09,bridge=vmbr0"})
    ctx = Ctx(plugin)
    _write_vm_restore_state(
        ctx, {**_vm_restore_state(ctx.service), "target_identity": {"nets": {"net0": TARGET_NET}, "name": "target-vm"}}
    )
    step = ReapplyTargetNetworkStep()
    assert _run(step.precheck(ctx)).result == StepResult.READY
    _run(step.execute(ctx))
    payload, deleted = plugin.set_calls[0]
    assert payload == {"net0": TARGET_NET, "name": "target-vm"}
    assert deleted == ["net1"]


def test_reapply_network_drops_the_sources_cloudinit_addresses():
    # The source had a private network on net1 with ipconfig1; the target only has net0.
    plugin = FakeVm(
        {
            "net0": "virtio=AA:BB:CC:00:00:01,bridge=vmbr0",
            "ipconfig0": "ip=10.0.0.5/24,gw=10.0.0.1",
            "net1": "virtio=AA:BB:CC:00:00:09,bridge=vmbr90,tag=3901",
            "ipconfig1": "ip=10.77.0.1/24",
        }
    )
    ctx = Ctx(plugin)
    target = {"net0": TARGET_NET, "ipconfig0": "ip=192.0.2.7/24,gw=192.0.2.1"}
    _write_vm_restore_state(
        ctx, {**_vm_restore_state(ctx.service), "target_identity": {"nets": target, "name": "target-vm"}}
    )
    _run(ReapplyTargetNetworkStep().execute(ctx))
    payload, deleted = plugin.set_calls[0]
    assert payload == {**target, "name": "target-vm"}
    assert deleted == ["ipconfig1", "net1"]


def test_reapply_network_refuses_without_captured_identity():
    assert _run(ReapplyTargetNetworkStep().precheck(Ctx(FakeVm({})))).result == StepResult.FAILED


def test_reapply_cloudinit_sets_targets_static_address():
    alloc = SimpleNamespace(
        ip_address="10.0.0.7", gateway="10.0.0.1", subnet_mask="255.255.255.0", bridge_name="vmbr1"
    )
    plugin = FakeVm({})
    ctx = Ctx(plugin, alloc=alloc)
    _run(ReapplyCloudInitStep().execute(ctx))
    ref, payload = plugin.configure_calls[0]
    assert ref == {"vmid": 5101}
    assert "10.0.0.7" in str(payload) and "10.0.0.1" in str(payload)


def test_reapply_cloudinit_clears_source_credentials_the_target_lacks():
    plugin = FakeVm({})
    _run(ReapplyCloudInitStep().execute(Ctx(plugin)))
    _ref, payload = plugin.configure_calls[0]
    assert "sshkeys" not in payload
    cleared = [delete for _payload, delete in plugin.set_calls]
    assert any("sshkeys" in delete for delete in cleared)


def test_reapply_cloudinit_failure_is_reported():
    plugin = FakeVm({})

    async def refuse(ref, payload):
        return False

    plugin.configure_vm = refuse
    try:
        _run(ReapplyCloudInitStep().execute(Ctx(plugin)))
    except DeploymentError as exc:
        assert "cloud-init" in str(exc)
    else:
        raise AssertionError("expected DeploymentError")
