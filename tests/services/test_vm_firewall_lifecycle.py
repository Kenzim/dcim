"""VM firewall lifecycle hooks: ``ApplyFirewallStep`` (its place in every VM strategy and what it
writes before first boot), restore completion, IP reassignment and admin ownership change."""

from types import SimpleNamespace

import pytest

from app.api.services_admin import ServiceOwnerAssignBody, assign_service_owner
from app.models.vm_firewall import ServiceFirewall
from app.models.vm_ip_allocation import VMIPAllocation
from app.services.deployment.registry import get_deployment_strategy_registry
from app.services.deployment.step import DeploymentError, StepResult
from app.services.deployment.steps import ApplyFirewallStep
from app.services.vm_firewall import proxmox_fw, reconcile
from app.services.vm_firewall import service as fw
from app.services.vm_firewall.proxmox_fw import net_firewall_flag
from app.services.vm_ip_reassign import reassign_vm_ip
from tests.fakes.proxmox_firewall import FakeProxmoxFirewall
from tests.fakes.vm_firewall_world import make_world

SSH = {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22"}
WEB = {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80"}

# Steps that set up the VM's NICs, size or cloud-init: the firewall step comes after all of them.
_SETUP_STEPS = {
    "configure_sizing",
    "configure_cloudinit_network",
    "reapply_target_network",
    "reapply_cloudinit",
    "reapply_macos_identity",
}
# Steps that start the guest.
_BOOT_STEPS = {"power_on", "finalize_backup_restore"}


@pytest.fixture
def world(db_session):
    return make_world(db_session)


@pytest.fixture
def fake(monkeypatch):
    fake = FakeProxmoxFirewall()
    monkeypatch.setattr(proxmox_fw, "PluginTransport", lambda plugin: None)
    monkeypatch.setattr(proxmox_fw, "VmFirewallClient", lambda transport, vmid: fake.client(vmid))
    return fake


def _ctx(world, vm, job_id=1):
    return SimpleNamespace(
        db=world.db,
        service=vm,
        job=SimpleNamespace(id=job_id),
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        require_placement=lambda: (vm.vm.proxmox_cluster_id, vm.vm.proxmox_node_name, vm.vm.proxmox_vmid),
        get_plugin=lambda: object(),
    )


async def _run_step(ctx):
    step = ApplyFirewallStep()
    outcome = await step.precheck(ctx)
    if outcome.result == StepResult.READY:
        await step.execute(ctx)
    return outcome


# Ordering in every strategy


def _vm_strategies():
    registry = get_deployment_strategy_registry()
    out = []
    for name in registry.names():
        names = registry.get(name).step_names()
        if "configure_sizing" in names:
            out.append((name, names))
    return out


def test_every_sizing_strategy_is_covered():
    names = {name for name, _ in _vm_strategies()}
    assert {"cloudinit_clone", "guest_agent", "macos_guest_agent", "windows_guest_agent",
            "restore_retained_backup", "restore_retained_backup_macos"} <= names


@pytest.mark.parametrize("name,steps", _vm_strategies(), ids=[name for name, _ in _vm_strategies()])
def test_apply_firewall_runs_after_setup_and_before_boot(name, steps):
    assert steps.count("apply_firewall") == 1
    at = steps.index("apply_firewall")
    assert all(steps.index(s) < at for s in _SETUP_STEPS if s in steps)
    boots = [steps.index(s) for s in _BOOT_STEPS if s in steps]
    assert boots and all(at < b for b in boots)


def test_strategies_without_sizing_have_no_firewall_step():
    registry = get_deployment_strategy_registry()
    for name in ("apply_guest_password", "restore_backup", "retry_restore_macos_configuration"):
        assert "apply_firewall" not in registry.get(name).step_names()


# What the step writes


@pytest.mark.asyncio
async def test_step_replaces_inherited_fw_sets_flag_and_blocks_smtp(world, fake):
    world.enable_location(smtp_block_new_vms=True, nic_flag_on_provision=True)
    vm = world.vm()
    fake_vm = fake.add_vm(vm.vm.proxmox_vmid)
    fake_vm.add_foreign_rule({"type": "in", "action": "ACCEPT", "proto": "tcp", "dport": "3389", "enable": 1})
    fake_vm.set_option("enable", 1)
    world.db.commit()

    outcome = await _run_step(_ctx(world, vm))
    assert outcome.result == StepResult.READY
    row = world.db.get(ServiceFirewall, vm.id)
    assert row.admin_overrides == {"smtp_out": "block"} and row.enforce_baseline
    assert row.applied_revision == row.revision and row.claimed_by is None
    assert net_firewall_flag(fake_vm.config["net0"])
    assert all(r.get("dport") != "3389" for r in fake_vm.rules)
    assert any(r.get("dport") == "25" and r.get("action") == "DROP" for r in fake_vm.rules)
    assert fake_vm.options["enable"] == 1

    # Idempotent: a retried step writes nothing new.
    writes = len(fake.writes)
    await _run_step(_ctx(world, vm))
    assert len(fake.writes) == writes


@pytest.mark.asyncio
async def test_step_writes_enable_0_in_a_disabled_location(world, fake):
    fw.update_location_settings(world.db, world.staff_actor, world.location.id, enabled=False)
    vm = world.vm()
    fake_vm = fake.add_vm(vm.vm.proxmox_vmid)
    fake_vm.set_option("enable", 1)
    world.db.commit()
    await _run_step(_ctx(world, vm))
    assert fake_vm.options["enable"] == 0
    assert not net_firewall_flag(fake_vm.config["net0"])
    assert world.db.get(ServiceFirewall, vm.id).admin_overrides in (None, {})


@pytest.mark.asyncio
async def test_step_skips_where_the_firewall_was_never_configured(db_session, fake):
    world = make_world(db_session, location_enabled=False)
    vm = world.vm()
    fake.add_vm(vm.vm.proxmox_vmid)
    world.db.commit()
    outcome = await _run_step(_ctx(world, vm))
    assert outcome.result == StepResult.SKIP and fake.writes == []
    assert world.db.get(ServiceFirewall, vm.id) is None


@pytest.mark.asyncio
async def test_step_leaves_an_infrastructure_vm_alone(world, fake):
    from app.services import settings_store

    settings_store.set_setting(world.db, "vm_firewall_infrastructure_bridges", "vmbr230")
    vm = world.vm()
    fake_vm = fake.add_vm(vm.vm.proxmox_vmid, net0="virtio=BC:24:11:00:00:09,bridge=vmbr230")
    world.db.commit()
    await _run_step(_ctx(world, vm))
    assert [w for w in fake.writes if f"/qemu/{fake_vm.vmid}/" in w[1]] == []


@pytest.mark.asyncio
async def test_step_fails_on_write_error_so_the_job_retries(world, fake):
    vm = world.vm()
    fake.add_vm(vm.vm.proxmox_vmid)
    world.db.commit()
    fake.fail_next(500, "Internal Server Error", count=10, method="PUT")
    with pytest.raises(DeploymentError, match="firewall apply failed"):
        await _run_step(_ctx(world, vm))
    assert world.db.get(ServiceFirewall, vm.id).claimed_by is None


@pytest.mark.asyncio
async def test_step_waits_for_a_worker_claim(world, fake):
    vm = world.vm()
    fake.add_vm(vm.vm.proxmox_vmid)
    fw.get_or_create(world.db, vm.id)
    world.db.commit()
    assert reconcile.claim(world.db, vm.id, owner="worker", now=reconcile.utcnow())
    with pytest.raises(DeploymentError, match="worker"):
        await _run_step(_ctx(world, vm))


# Other lifecycle hooks


@pytest.mark.asyncio
async def test_ip_reassignment_bumps_before_the_guest_moves(world):
    vm = world.vm()
    row = fw.get_or_create(world.db, vm.id)
    new = VMIPAllocation(ip_address="198.51.100.77", subnet_mask="255.255.255.0", gateway="198.51.100.1")
    world.db.add(new)
    world.db.commit()
    before = row.revision
    await reassign_vm_ip(world.db, vm, allocation_id=new.id, reset_network=False)
    world.db.refresh(row)
    assert row.revision == before + 1


@pytest.mark.asyncio
async def test_admin_ownership_change_removes_foreign_references(world):
    vm = world.vm("alice")
    group = fw.create_group(world.db, world.actor("alice"), name="a", rules=[SSH])
    fw.replace_rules(world.db, vm.id, world.actor("alice"), [WEB, {"kind": "group", "group_id": group.id}],
                     revision=fw.get_or_create(world.db, vm.id).revision)
    world.db.commit()
    bob = world.user("bob")
    await assign_service_owner(vm.id, ServiceOwnerAssignBody(owner_user_id=bob.id), {"user_id": world.staff.id},
                               world.db)
    assert [r.ports for r in fw.service_rules(world.db, vm.id)] == ["80"]
    from app.models.commerce_audit import UserAuditEvent

    audit = world.db.query(UserAuditEvent).filter_by(action="firewall.references_removed").one()
    assert audit.actor_user_id == world.staff.id
