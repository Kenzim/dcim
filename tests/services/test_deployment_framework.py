"""
Unit + integration tests for the VM deployment step/strategy/runner framework.

Covers:
- strategy step lists (both install types)
- step precheck outcomes (ready / wait / skip / failed)
- runner: skip advances, wait persists next_run_at, ready executes + advances,
  a full cloudinit run reaches success, and clone is idempotent (skip on re-tick).
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.dao.service_dao import ServiceDAO
from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
from app.models.product_catalog import VMTemplate
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.service_vm import VMGuestState
from app.models.vm_deployment_job import DeploymentJobStatus, DeploymentStepStatus
from app.plugins.base import PowerState
from app.services.deployment.context import DeploymentContext
from app.services.deployment.registry import get_deployment_strategy_registry
from app.services.deployment.runner import run_job_tick
from app.services.deployment.step import StepResult
from app.services.deployment.steps import (
    CloneFromTemplateStep,
    ConfigureCloudInitNetworkStep,
    PowerOnStep,
    WaitForGuestAgentStep,
)
from app.services.deployment.strategies import (
    CloudinitCloneStrategy,
    GuestAgentStrategy,
    WindowsGuestAgentStrategy,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakePlugin:
    def __init__(self, *, exists=False, power=PowerState.OFF, agent=False):
        self._exists = exists
        self._power = power
        self._agent = agent
        self.vmid = 5000
        self.calls = []
        self._qemu_config = {}

    async def vm_exists(self):
        return self._exists

    async def clone_vm_from_template(self, template_ref, vm_config):
        self.calls.append(("clone", dict(vm_config)))
        self.last_template_ref = dict(template_ref)
        self._exists = True
        return {"vmid": vm_config.get("vmid"), "task": None}

    async def wait_for_proxmox_task(self, upid):
        return None

    async def configure_vm(self, vm_ref, vm_config):
        self.calls.append(("configure", vm_config))
        return True

    async def ensure_network_bridge(self, bridge, *, vmid=None, net_key="net0"):
        self.calls.append(("ensure_network_bridge", bridge, net_key, vmid))
        return {"changed": True, "net_key": net_key, "value": f"virtio=AA:BB:CC:DD:EE:FF,bridge={bridge}"}

    async def regenerate_cloudinit(self, vmid=None):
        self.calls.append(("regenerate_cloudinit", vmid))

    async def get_power_state(self):
        return self._power

    async def power_on(self):
        self.calls.append("power_on")
        self._power = PowerState.ON
        return True

    async def power_off(self, force=False):
        self.calls.append(("power_off", force))
        self._power = PowerState.OFF
        return True

    async def guest_shutdown(self):
        self.calls.append("guest_shutdown")
        self._power = PowerState.OFF

    async def guest_agent_ready(self):
        return self._agent

    async def guest_exec(self, command, input_data=None, timeout=180.0):
        self.calls.append(("guest_exec", list(command)))
        return {"exitcode": 0, "out-data": "", "err-data": ""}

    async def guest_set_user_password(self, username, password, old_password=None, old_passwords=None):
        self.calls.append(("set_password", username))
        return None

    async def get_qemu_config(self, vmid=None):
        return dict(self._qemu_config)

    async def update_smbios1(self, smbios1: str):
        self.calls.append(("update_smbios1", smbios1))
        self._qemu_config["smbios1"] = smbios1

    async def update_description(self, description: str):
        self.calls.append(("update_description", description))
        self._qemu_config["description"] = description


def _fake_alloc():
    return SimpleNamespace(
        ip_address="192.0.2.10",
        gateway="192.0.2.1",
        subnet_mask="255.255.255.0",
        bridge_name=None,
    )


class FakeCtx:
    """Minimal duck-typed context for step precheck/execute unit tests."""

    def __init__(self, plugin, *, alloc=None, specs=None, config=None):
        self._plugin = plugin
        self._alloc = alloc
        self._specs = specs or {"memory_mb": 2048, "cores": 2, "full_clone": True}
        self.service = SimpleNamespace(name="unit-vm", config=config or {})
        self.logger = SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            debug=lambda *a, **k: None,
        )

    def get_plugin(self):
        return self._plugin

    def get_ip_allocation(self):
        return self._alloc

    def get_specs(self):
        return dict(self._specs)

    def require_placement(self):
        return (1, "pve1", 5000)

    async def resolve_template_location(self):
        return ("pve1", 9000)

    async def resolve_template_vmid(self):
        _node, vmid = await self.resolve_template_location()
        return vmid


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Strategy step lists
# --------------------------------------------------------------------------- #
def test_cloudinit_strategy_step_order():
    assert CloudinitCloneStrategy().step_names() == [
        "clone_from_template",
        "configure_sizing",
        "configure_cloudinit_network",
        "power_on",
        "stamp_vm_identity",
    ]


def test_guest_agent_strategy_step_order():
    assert GuestAgentStrategy().step_names() == [
        "clone_from_template",
        "configure_sizing",
        "power_on",
        "wait_for_guest_agent",
        "configure_via_guest_agent",
        "stamp_vm_identity",
    ]


def test_windows_guest_agent_strategy_step_order():
    assert WindowsGuestAgentStrategy().step_names() == [
        "clone_from_template",
        "configure_sizing",
        "power_on",
        "wait_for_guest_agent",
        "configure_windows_guest",
        "stamp_vm_identity",
    ]


def test_registry_resolves_known_and_unknown():
    reg = get_deployment_strategy_registry()
    assert reg.resolve("cloudinit_clone") is not None
    assert reg.resolve("guest_agent") is not None
    assert reg.resolve("windows_guest_agent") is not None
    assert reg.resolve("apply_guest_password") is not None
    assert reg.resolve("apply_guest_password").mutates_provision_lifecycle is False
    assert reg.resolve("nope") is None
    with pytest.raises(ValueError):
        reg.get("nope")


# --------------------------------------------------------------------------- #
# Step precheck outcomes
# --------------------------------------------------------------------------- #
def test_clone_precheck_skips_when_vm_exists():
    ctx = FakeCtx(FakePlugin(exists=True))
    outcome = _run(CloneFromTemplateStep().precheck(ctx))
    assert outcome.result == StepResult.SKIP


def test_clone_precheck_ready_when_absent():
    ctx = FakeCtx(FakePlugin(exists=False))
    outcome = _run(CloneFromTemplateStep().precheck(ctx))
    assert outcome.result == StepResult.READY


def test_clone_defaults_to_linked_clone():
    plugin = FakePlugin(exists=False)
    ctx = FakeCtx(plugin, specs={"memory_mb": 2048, "cores": 2})
    _run(CloneFromTemplateStep().execute(ctx))
    assert plugin.calls == [("clone", {"vmid": 5000, "name": "unit-vm", "full_clone": False})]


def test_clone_honors_full_clone_opt_in():
    plugin = FakePlugin(exists=False)
    ctx = FakeCtx(plugin, specs={"memory_mb": 2048, "cores": 2, "full_clone": True})
    _run(CloneFromTemplateStep().execute(ctx))
    assert plugin.calls == [("clone", {"vmid": 5000, "name": "unit-vm", "full_clone": True})]


def test_clone_sets_target_node_when_template_on_other_node():
    plugin = FakePlugin(exists=False)
    ctx = FakeCtx(plugin, specs={"memory_mb": 2048, "cores": 2, "full_clone": False})

    async def _loc():
        return ("pve-template", 9000)

    ctx.resolve_template_location = _loc  # type: ignore[method-assign]
    # Placement remains pve1; template lives elsewhere (shared storage).
    _run(CloneFromTemplateStep().execute(ctx))
    assert plugin.calls == [
        (
            "clone",
            {
                "vmid": 5000,
                "name": "unit-vm",
                "full_clone": False,
                "target_node": "pve1",
            },
        )
    ]
    assert plugin.last_template_ref == {"vmid": 9000, "node": "pve-template"}


def test_power_on_precheck_skips_when_running():
    ctx = FakeCtx(FakePlugin(power=PowerState.ON))
    outcome = _run(PowerOnStep().precheck(ctx))
    assert outcome.result == StepResult.SKIP


def test_cloudinit_network_precheck_fails_without_allocation():
    ctx = FakeCtx(FakePlugin(), alloc=None)
    outcome = _run(ConfigureCloudInitNetworkStep().precheck(ctx))
    assert outcome.result == StepResult.FAILED


def test_cloudinit_network_precheck_ready_with_allocation():
    ctx = FakeCtx(FakePlugin(), alloc=_fake_alloc())
    outcome = _run(ConfigureCloudInitNetworkStep().precheck(ctx))
    assert outcome.result == StepResult.READY


def test_cloudinit_network_maps_template_password_to_root():
    plugin = FakePlugin()
    ctx = FakeCtx(
        plugin,
        alloc=_fake_alloc(),
        specs={"memory_mb": 2048, "cores": 2},
        config={
            "template_parameters": {
                "admin_password": "secret123",
                "admin_username": "ubuntu",  # ignored — cloud-init always uses root
            },
            "vm_plan": {
                "strategy_plan": {"strategy_config": {"guest_username": "ubuntu"}},
                "effective_specs": {},
            },
        },
    )
    _run(ConfigureCloudInitNetworkStep().execute(ctx))
    configure_calls = [c for c in plugin.calls if isinstance(c, tuple) and c[0] == "configure"]
    assert configure_calls
    payload = configure_calls[0][1]
    assert payload["ciuser"] == "root"
    assert payload["cipassword"] == "secret123"
    assert payload["ipconfig0"] == "ip=192.0.2.10/24,gw=192.0.2.1"
    assert payload["nameserver"] == "1.1.1.1 8.8.8.8"


def test_wait_for_guest_agent_waits_then_ready():
    plugin = FakePlugin(agent=False)
    ctx = FakeCtx(plugin)
    step = WaitForGuestAgentStep()
    waiting = _run(step.precheck(ctx))
    assert waiting.result == StepResult.WAIT
    assert waiting.retry_after_seconds  # a backoff is suggested
    plugin._agent = True
    ready = _run(step.precheck(ctx))
    assert ready.result == StepResult.READY


# --------------------------------------------------------------------------- #
# Runner integration
# --------------------------------------------------------------------------- #
def _placed_vm_service(db_session, strategy_name):
    tmpl = VMTemplate(
        code="ubuntu-2204",
        name="U",
        os_type="Linux - Cloudinit",
        proxmox_template_name="ubuntu-2204",
    )
    db_session.add(tmpl)
    db_session.commit()
    db_session.refresh(tmpl)
    service = ServiceDAO.create_vm(
        db_session,
        name="vm-run-1",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
        vm_template_id=tmpl.id,
        proxmox_cluster_id=1,
        proxmox_node_name="pve1",
    )
    service.vm.proxmox_vmid = 5000
    service.config = {
        "vm_plan": {
            "strategy_name": strategy_name,
            "effective_specs": {"memory_mb": 2048, "cores": 2, "full_clone": True},
        }
    }
    ServiceDAO.update(db_session, service)
    return service


def _patch_context(monkeypatch, plugin, alloc=None):
    monkeypatch.setattr(DeploymentContext, "get_plugin", lambda self: plugin)
    monkeypatch.setattr(DeploymentContext, "get_ip_allocation", lambda self: alloc)

    async def _tmpl_loc(self):
        return ("pve1", 9000)

    async def _tmpl(self):
        return 9000

    monkeypatch.setattr(DeploymentContext, "resolve_template_location", _tmpl_loc)
    monkeypatch.setattr(DeploymentContext, "resolve_template_vmid", _tmpl)


def _create_job(db_session, service, strategy_name):
    strategy = get_deployment_strategy_registry().get(strategy_name)
    return VMDeploymentJobDAO.create_job(
        db_session,
        service_id=service.id,
        strategy_name=strategy_name,
        step_names=strategy.step_names(),
        max_attempts=strategy.max_attempts,
    )


def _tick_once(db_session):
    job = VMDeploymentJobDAO.claim_next_runnable(db_session, worker_id="w1", lease_ttl_seconds=60)
    if job is None:
        return None
    _run(run_job_tick(db_session, job))
    db_session.expire_all()
    return VMDeploymentJobDAO.get_by_id(db_session, job.id)


def _drive(db_session, max_ticks=30):
    last = None
    for _ in range(max_ticks):
        job = _tick_once(db_session)
        if job is None:
            return last
        last = job
        if job.status in (
            DeploymentJobStatus.SUCCEEDED,
            DeploymentJobStatus.FAILED,
            DeploymentJobStatus.CANCELLED,
            # Job-level step retries park here with next_run_at set; stop so tests
            # can assert waiting/retry instead of spinning forever.
            DeploymentJobStatus.WAITING,
        ):
            return job
    return last


def test_runner_cloudinit_runs_to_success(db_session, monkeypatch):
    plugin = FakePlugin(exists=False, power=PowerState.OFF)
    _patch_context(monkeypatch, plugin, alloc=_fake_alloc())
    service = _placed_vm_service(db_session, "cloudinit_clone")
    _create_job(db_session, service, "cloudinit_clone")

    job = _drive(db_session)
    assert job.status == DeploymentJobStatus.SUCCEEDED
    steps = VMDeploymentJobDAO.list_steps(db_session, job.id)
    assert all(
        s.status in (DeploymentStepStatus.SUCCEEDED, DeploymentStepStatus.SKIPPED) for s in steps
    )
    assert any(c == "clone" or (isinstance(c, tuple) and c[0] == "clone") for c in plugin.calls)
    assert "power_on" in plugin.calls
    # Service mirrors: active + running guest.
    db_session.refresh(service)
    assert service.status == ServiceStatus.ACTIVE
    assert service.vm.guest_state == VMGuestState.RUNNING


def test_runner_skips_clone_when_vm_exists(db_session, monkeypatch):
    plugin = FakePlugin(exists=True, power=PowerState.OFF)
    _patch_context(monkeypatch, plugin, alloc=_fake_alloc())
    service = _placed_vm_service(db_session, "cloudinit_clone")
    _create_job(db_session, service, "cloudinit_clone")

    job = _drive(db_session)
    assert job.status == DeploymentJobStatus.SUCCEEDED
    # Clone never invoked because the VMID already existed.
    assert not any(c == "clone" or (isinstance(c, tuple) and c[0] == "clone") for c in plugin.calls)
    clone_step = next(
        s for s in VMDeploymentJobDAO.list_steps(db_session, job.id) if s.name == "clone_from_template"
    )
    assert clone_step.status == DeploymentStepStatus.SKIPPED


def test_runner_guest_agent_waits_then_completes(db_session, monkeypatch):
    plugin = FakePlugin(exists=False, power=PowerState.OFF, agent=False)
    _patch_context(monkeypatch, plugin, alloc=_fake_alloc())
    service = _placed_vm_service(db_session, "guest_agent")
    _create_job(db_session, service, "guest_agent")

    # Drive until it parks in WAITING at the guest-agent step.
    waiting_job = None
    for _ in range(10):
        job = _tick_once(db_session)
        if job is None:
            break
        if job.status == DeploymentJobStatus.WAITING:
            waiting_job = job
            break
    assert waiting_job is not None
    assert waiting_job.next_run_at is not None
    wait_step = VMDeploymentJobDAO.get_step(db_session, waiting_job.id, waiting_job.current_step_index)
    assert wait_step.name == "wait_for_guest_agent"
    assert wait_step.status == DeploymentStepStatus.WAITING

    # Agent comes up + clear the backoff so the next claim is due; job completes.
    plugin._agent = True
    waiting_job.next_run_at = None
    db_session.commit()
    job = _drive(db_session)
    assert job.status == DeploymentJobStatus.SUCCEEDED


def test_runner_retries_on_missing_ip_allocation(db_session, monkeypatch):
    """Transient/config step failures park the job in WAITING (24h budget), not FAILED."""
    plugin = FakePlugin(exists=True, power=PowerState.OFF)  # skip clone
    _patch_context(monkeypatch, plugin, alloc=None)  # cloudinit net precheck fails
    service = _placed_vm_service(db_session, "cloudinit_clone")
    _create_job(db_session, service, "cloudinit_clone")

    job = _drive(db_session)
    assert job.status == DeploymentJobStatus.WAITING
    assert job.next_run_at is not None
    assert job.error_message
    db_session.refresh(service)
    assert service.vm.guest_state == VMGuestState.PROVISIONING
    assert service.vm.guest_last_error


def test_runner_fails_after_retry_budget(db_session, monkeypatch):
    """Once the 24h job budget elapses, a step failure becomes terminal FAILED."""
    from datetime import timedelta

    from app.services.deployment import runner as runner_mod

    plugin = FakePlugin(exists=True, power=PowerState.OFF)
    _patch_context(monkeypatch, plugin, alloc=None)
    service = _placed_vm_service(db_session, "cloudinit_clone")
    job = _create_job(db_session, service, "cloudinit_clone")
    # Pretend the job started just outside the retry budget.
    job.started_at = runner_mod._utcnow() - runner_mod.JOB_RETRY_BUDGET - timedelta(minutes=1)
    db_session.commit()

    job = _drive(db_session)
    assert job.status == DeploymentJobStatus.FAILED
    assert "gave up after" in (job.error_message or "")
    db_session.refresh(service)
    assert service.vm.guest_state == VMGuestState.ERROR
