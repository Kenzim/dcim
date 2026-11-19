"""Backup slots: retaining, protection, quota and purge handling, lifecycle, and restore onto another VM."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.dao.backup_slot_dao import RetainedBackupDAO
from app.dao.service_dao import ServiceDAO
from app.models.backup_slot import BackupSlot
from app.models.product_catalog import VMTemplate
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.models.user import User
from app.services import backup_slot_service as slots
from app.services.backup_slot_service import GIB, BackupSlotError
from app.services.provisioning import ProvisioningActor, ProvisioningService, ProvisionRequest
from app.services.provisioning.errors import ProvisioningError
from app.services.retained_restore_service import restore_retained_onto
from app.services.vm_backup_service import (
    BackupForbiddenError,
    BackupQuotaError,
    create_client_backup,
    delete_client_backup,
    list_service_backups,
    purge_client_backups,
)

ACTOR = ProvisioningActor(kind="admin", actor_id=1, name="test", source="test")
STORAGE = "pbs-client"
SPECS = {
    "platform_backup_storage": "pbs-platform",
    "client_backup_storage": STORAGE,
    "max_client_backups": 1,
    "memory_mb": 2048,
    "cores": 2,
}


class FakePbs:
    """Proxmox plugin double: a backup catalogue with protection, restore and storage listing."""

    def __init__(self):
        self.backups = []
        self.protected = set()
        self.deleted = []
        self.protect_calls = []
        self.restore_calls = []
        self.config = {"scsi0": "local-lvm:vm-1-disk-0,size=20G", "net0": "virtio=AA:BB:CC:00:00:01,bridge=vmbr0"}
        self.storages = [{"storage": STORAGE, "active": True, "type": "pbs"}]
        self.delete_error = None

    def add(self, vmid, name, size=5 * GIB, notes="rf1:tpl=debian-13;svc=1", ctime=1000):
        row = {"volid": f"{STORAGE}:backup/vm/{vmid}/{name}", "vmid": vmid, "size": size, "ctime": ctime, "notes": notes}
        self.backups.append(row)
        return row["volid"]

    async def list_backups(self, storage, vmid=None):
        return [dict(b) for b in self.backups if vmid is None or b["vmid"] == vmid]

    async def set_backup_protected(self, storage, volume, protected):
        self.protect_calls.append((storage, volume, protected))
        (self.protected.add if protected else self.protected.discard)(volume)

    async def delete_backup(self, storage, volume):
        if volume in self.protected:
            raise RuntimeError("volume is protected")
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(volume)
        self.backups = [b for b in self.backups if b["volid"] != volume]

    async def extract_backup_config(self, volid):
        return "scsi0: local-lvm:vm-1-disk-0,size=20G\n"

    async def get_qemu_config(self, vmid=None):
        return dict(self.config)

    async def list_node_storages(self, content="backup"):
        return list(self.storages)

    async def restore_backup(self, **kwargs):
        self.restore_calls.append(kwargs)
        return "UPID:restore"


@pytest.fixture
def world(db_session):
    cluster = ProxmoxCluster(
        name="slots", api_url="https://pve.example:8006", username="root@pam", password="x",
        verify_ssl=False, vmid_min=5000, vmid_max=6000,
    )
    db_session.add(cluster)
    users = {}
    for name in ("alice", "bob"):
        user = User(username=name, email=f"{name}@example.com", is_admin=False)
        user.set_password("secret123")
        db_session.add(user)
        users[name] = user
    for code, label, os_type in (
        ("debian-13", "Debian 13", "Linux - Cloudinit"),
        ("macos-14", "macOS 14", "macOS - Guest agent"),
        ("win-2022", "Windows 2022", "Windows - Guest agent"),
    ):
        db_session.add(VMTemplate(code=code, name=label, os_type=os_type, proxmox_template_name=code))
    db_session.commit()

    def vm(owner, name, vmid, strategy="cloudinit_clone"):
        service = ServiceDAO.create_vm(
            db_session, name=name, owner_user_id=owner.id, provisioning_source=ProvisioningSource.INTERNAL,
            status=ServiceStatus.ACTIVE, proxmox_cluster_id=cluster.id, proxmox_node_name="pve",
            proxmox_vmid=vmid, product_code="p", product_snapshot={"effective_specs": dict(SPECS)},
            config={"vm_plan": {"strategy_name": strategy, "effective_specs": dict(SPECS)}},
        )
        return service

    def slot(owner, size_gb=50, name="slot"):
        return ProvisioningService.create(
            db_session,
            ProvisionRequest(
                name=name, service_type=ServiceType.BACKUP_SLOT, owner_user_id=owner.id,
                provisioning_source=ProvisioningSource.INTERNAL, size_gb=size_gb,
            ),
            ACTOR,
        )

    pbs = FakePbs()
    w = type("World", (), {})()
    w.cluster, w.users, w.pbs, w.vm, w.slot = cluster, users, pbs, vm, slot
    w.vm1 = vm(users["alice"], "vm-one", 5100)
    w.vm2 = vm(users["alice"], "vm-two", 5101)
    w.bob_vm = vm(users["bob"], "bob-vm", 5200)
    w.slot1 = slot(users["alice"])

    async def plugin_for(db, service):
        return pbs, cluster.id, "pve", service.vm.proxmox_vmid

    patches = [
        patch("app.services.vm_backup_service.get_vm_backup_plugin", new=AsyncMock(side_effect=plugin_for)),
        patch("app.services.vm_backup_service.list_running_backup_jobs", new=AsyncMock(return_value=[])),
        patch("app.services.backup_slot_service.get_cluster_plugin", new=lambda *a, **k: pbs),
    ]
    for p in patches:
        p.start()
    yield w
    for p in patches:
        p.stop()


def _reload(db, service):
    db.expire_all()
    return ServiceDAO.get_by_id(db, service.id)


# --- provisioning and sizes ---


def test_slot_is_active_with_size_and_no_server(world, db_session):
    slot = world.slot1
    assert slot.service_type == ServiceType.BACKUP_SLOT
    assert slot.status == ServiceStatus.ACTIVE
    row = db_session.query(BackupSlot).filter_by(service_id=slot.id).one()
    assert row.size_bytes == 50 * GIB and row.retained is None


def test_slot_needs_valid_size_and_owner(world, db_session):
    with pytest.raises(ProvisioningError):
        ProvisioningService.create(
            db_session,
            ProvisionRequest(name="x", service_type=ServiceType.BACKUP_SLOT, owner_user_id=world.users["alice"].id,
                             provisioning_source=ProvisioningSource.INTERNAL),
            ACTOR,
        )


# --- retaining ---


@pytest.mark.asyncio
async def test_retain_sets_protected_flag_and_records_identity(world, db_session):
    volid = world.pbs.add(5100, "a")
    out = await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid, name="before upgrade")
    assert world.pbs.protect_calls == [(STORAGE, volid, True)]
    assert volid in world.pbs.protected
    assert out["volid"] == volid and out["name"] == "before upgrade"
    assert out["template_code"] == "debian-13" and out["os_type"] == "Linux - Cloudinit"
    row = RetainedBackupDAO.get_by_volid(db_session, volid)
    assert row.owner_user_id == world.users["alice"].id
    assert row.source_service_id == world.vm1.id and row.source_vmid == 5100
    assert row.disk_bytes == 20 * GIB and row.proxmox_cluster_id == world.cluster.id


@pytest.mark.asyncio
async def test_retain_refuses_backup_bigger_than_slot(world, db_session):
    small = world.slot(world.users["alice"], size_gb=2, name="small")
    volid = world.pbs.add(5100, "big", size=5 * GIB)
    with pytest.raises(BackupSlotError, match="at most 2 GB"):
        await slots.retain_from_vm(db_session, small, world.vm1, volid=volid)
    assert world.pbs.protect_calls == []


@pytest.mark.asyncio
async def test_retain_needs_an_empty_slot_unless_replacing(world, db_session):
    first = world.pbs.add(5100, "a")
    second = world.pbs.add(5100, "b", ctime=2000)
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=first)
    with pytest.raises(BackupSlotError, match="already holds"):
        await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=second)
    out = await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=second, replace=True)
    assert out["volid"] == second and out["replaced_cleanup_failed"] is False
    assert first in world.pbs.deleted and first not in world.pbs.protected
    assert second in world.pbs.protected
    assert RetainedBackupDAO.get_by_volid(db_session, first) is None


@pytest.mark.asyncio
async def test_retain_across_clients_is_refused(world, db_session):
    volid = world.pbs.add(5200, "b")
    with pytest.raises(BackupSlotError) as err:
        await slots.retain_from_vm(db_session, world.slot1, world.bob_vm, volid=volid)
    assert err.value.status_code == 403
    assert world.pbs.protect_calls == []


@pytest.mark.asyncio
async def test_retain_unknown_backup_and_platform_backup(world, db_session):
    with pytest.raises(BackupSlotError, match="not found"):
        await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=f"{STORAGE}:backup/vm/5100/nope")
    with pytest.raises(BackupSlotError, match="client backups"):
        await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid="pbs-platform:backup/vm/5100/x")


@pytest.mark.asyncio
async def test_retain_blocked_while_slot_suspended_or_terminated(world, db_session):
    volid = world.pbs.add(5100, "a")
    world.slot1.status = ServiceStatus.SUSPENDED
    db_session.commit()
    with pytest.raises(BackupSlotError, match="suspended"):
        await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    world.slot1.status = ServiceStatus.ACTIVE
    db_session.commit()
    slots.mark_terminated(db_session, world.slot1.id)
    with pytest.raises(BackupSlotError, match="terminated"):
        await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)


@pytest.mark.asyncio
async def test_failed_protect_leaves_no_row(world, db_session):
    volid = world.pbs.add(5100, "a")

    async def boom(*a, **k):
        raise RuntimeError("pbs down")

    world.pbs.set_backup_protected = boom
    with pytest.raises(BackupSlotError, match="Could not protect"):
        await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    assert RetainedBackupDAO.get_by_volid(db_session, volid) is None


# --- interaction with the normal backup flow ---


@pytest.mark.asyncio
async def test_retained_backup_is_listed_protected_and_not_deletable(world, db_session):
    kept = world.pbs.add(5100, "kept")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=kept)
    items = await list_service_backups(db_session, world.vm1)
    item = next(i for i in items if i["volid"] == kept)
    assert item["retained"] is True and item["deletable"] is False and item["protected"] is True
    assert item["slot_service_id"] == world.slot1.id
    with pytest.raises(BackupForbiddenError, match="retained in a backup slot"):
        await delete_client_backup(db_session, world.vm1, volid=kept, storage=STORAGE)


@pytest.mark.asyncio
async def test_retained_backup_does_not_count_towards_quota(world, db_session):
    kept = world.pbs.add(5100, "kept")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=kept)
    plugin_create = AsyncMock(return_value="UPID:create")
    world.pbs.create_backup = plugin_create
    world.pbs.list_tasks = AsyncMock(return_value=[])
    # max_client_backups is 1 and one backup exists, but it is retained, so a new one is allowed.
    await create_client_backup(db_session, world.vm1, notes="n", mode="snapshot", wait=False)
    assert plugin_create.await_count == 1
    other = world.pbs.add(5100, "ordinary", ctime=3000)
    with pytest.raises(BackupQuotaError):
        await create_client_backup(db_session, world.vm1, notes="n", mode="snapshot", wait=False)
    assert other


@pytest.mark.asyncio
async def test_purge_client_backups_skips_retained(world, db_session):
    kept = world.pbs.add(5100, "kept")
    ordinary = world.pbs.add(5100, "ordinary", ctime=3000)
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=kept)
    await purge_client_backups(db_session, world.vm1)
    assert ordinary in world.pbs.deleted
    assert kept not in world.pbs.deleted and kept in world.pbs.protected


@pytest.mark.asyncio
async def test_another_vm_reusing_the_vmid_does_not_see_the_retained_backup(world, db_session):
    kept = world.pbs.add(5100, "kept")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=kept)
    db_session.delete(_reload(db_session, world.vm1))
    db_session.commit()
    impostor = world.vm(world.users["alice"], "reuses-5100", 5100)
    assert all(i["volid"] != kept for i in await list_service_backups(db_session, impostor))


# --- release, resize, lifecycle ---


@pytest.mark.asyncio
async def test_release_unprotects_then_deletes(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    out = await slots.release_from_slot(db_session, world.slot1)
    assert out["released"] is True
    assert world.pbs.protect_calls[-1] == (STORAGE, volid, False)
    assert volid in world.pbs.deleted
    assert RetainedBackupDAO.get_by_volid(db_session, volid) is None
    with pytest.raises(BackupSlotError, match="holds no backup"):
        await slots.release_from_slot(db_session, world.slot1)


@pytest.mark.asyncio
async def test_release_keeps_the_record_when_storage_delete_fails(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    world.pbs.delete_error = RuntimeError("pbs unreachable")
    with pytest.raises(BackupSlotError, match="Could not delete"):
        await slots.release_from_slot(db_session, world.slot1)
    assert RetainedBackupDAO.get_by_volid(db_session, volid) is not None
    assert volid in world.pbs.protected


@pytest.mark.asyncio
async def test_release_does_not_treat_a_missing_storage_as_a_missing_volume(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    world.pbs.delete_error = RuntimeError(f"storage '{STORAGE}' does not exist")
    with pytest.raises(BackupSlotError, match="Could not delete"):
        await slots.release_from_slot(db_session, world.slot1)
    assert RetainedBackupDAO.get_by_volid(db_session, volid) is not None
    assert volid in world.pbs.protected


@pytest.mark.asyncio
async def test_release_counts_an_already_deleted_volume_as_done(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    world.pbs.delete_error = RuntimeError(f"volume '{volid}' does not exist")
    out = await slots.release_from_slot(db_session, world.slot1)
    assert out["released"] is True
    assert RetainedBackupDAO.get_by_volid(db_session, volid) is None


@pytest.mark.asyncio
async def test_resize_refuses_below_the_retained_backup(world, db_session):
    volid = world.pbs.add(5100, "a", size=10 * GIB)
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    with pytest.raises(BackupSlotError, match="release it before shrinking"):
        slots.resize(db_session, world.slot1, 5)
    assert slots.resize(db_session, world.slot1, 80).size_bytes == 80 * GIB
    assert slots.resize(db_session, world.slot1, 10).size_bytes == 10 * GIB


@pytest.mark.asyncio
async def test_termination_starts_grace_then_purges(world, db_session, monkeypatch):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    monkeypatch.setattr(settings, "backup_slot_destroy_grace_seconds", 3600)
    world.slot1.status = ServiceStatus.TERMINATED
    db_session.commit()
    assert slots.reconcile_terminations(db_session) == 1
    slot = db_session.query(BackupSlot).filter_by(service_id=world.slot1.id).one()
    assert slot.terminated_at is not None
    assert slot.destroy_after - slot.terminated_at.replace(tzinfo=slot.destroy_after.tzinfo) == timedelta(hours=1)

    assert await slots.purge_expired(db_session) == 0
    assert volid not in world.pbs.deleted and volid in world.pbs.protected

    slot.destroy_after = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    assert await slots.purge_expired(db_session) == 1
    assert volid in world.pbs.deleted
    assert db_session.query(BackupSlot).filter_by(service_id=world.slot1.id).first() is None


@pytest.mark.asyncio
async def test_purge_waits_for_a_running_restore_from_the_backup(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    await slots.restore_from_slot(db_session, world.slot1, world.vm2, confirm_data_loss=True)
    world.slot1.status = ServiceStatus.TERMINATED
    db_session.commit()
    slots.reconcile_terminations(db_session)
    slot = db_session.query(BackupSlot).filter_by(service_id=world.slot1.id).one()
    slot.destroy_after = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    assert await slots.purge_expired(db_session) == 0
    assert volid not in world.pbs.deleted and volid in world.pbs.protected


def test_empty_slot_terminates_without_grace(world, db_session):
    slots.mark_terminated(db_session, world.slot1.id)
    slot = db_session.query(BackupSlot).filter_by(service_id=world.slot1.id).one()
    assert slot.destroy_after is not None and slot.destroy_after <= slot.destroy_after.__class__.now(slot.destroy_after.tzinfo)


@pytest.mark.asyncio
async def test_reinstated_slot_cancels_destruction(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    slots.mark_terminated(db_session, world.slot1.id)
    slots.mark_reinstated(db_session, world.slot1.id)
    slot = db_session.query(BackupSlot).filter_by(service_id=world.slot1.id).one()
    assert slot.terminated_at is None and slot.destroy_after is None


@pytest.mark.asyncio
async def test_retained_backup_survives_source_vm_termination(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    await purge_client_backups(db_session, world.vm1)
    db_session.delete(_reload(db_session, world.vm1))
    db_session.commit()
    row = RetainedBackupDAO.get_by_volid(db_session, volid)
    # The row keeps the VM's name for display; the source id is set NULL by the database on delete.
    assert row is not None and row.source_service_name == "vm-one"
    assert volid in world.pbs.protected


# --- restore onto another VM ---


@pytest.mark.asyncio
async def test_restore_onto_other_vm_requires_confirmation(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    with pytest.raises(BackupSlotError, match="replaces everything on that VM") as err:
        await slots.restore_from_slot(db_session, world.slot1, world.vm2)
    assert err.value.status_code == 409
    assert world.pbs.restore_calls == []


@pytest.mark.asyncio
async def test_restore_onto_other_vm_queues_the_retained_job(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    out = await slots.restore_from_slot(db_session, world.slot1, world.vm2, confirm_data_loss=True)
    assert out["status"] == "queued" and out["target_service_id"] == world.vm2.id
    vm2 = _reload(db_session, world.vm2)
    state = vm2.config["vm_restore"]
    assert state["volid"] == volid and state["kind"] == "retained" and state["job_id"] == out["job_id"]
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO

    job = VMDeploymentJobDAO.get_by_id(db_session, out["job_id"])
    assert job.strategy_name == "restore_retained_backup"


@pytest.mark.asyncio
async def test_restore_onto_other_clients_vm_is_refused(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    with pytest.raises(BackupSlotError) as err:
        await slots.restore_from_slot(db_session, world.slot1, world.bob_vm, confirm_data_loss=True)
    assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_restore_refused_when_target_disk_is_smaller(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    world.pbs.config = {"scsi0": "local-lvm:vm-2-disk-0,size=10G"}
    with pytest.raises(BackupSlotError, match="disk is 10 GB but the backup's disk is 20 GB"):
        await slots.restore_from_slot(db_session, world.slot1, world.vm2, confirm_data_loss=True)


@pytest.mark.asyncio
async def test_restore_refused_when_storage_unreachable_from_target_node(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    world.pbs.storages = [{"storage": STORAGE, "active": False, "type": "pbs"}]
    with pytest.raises(BackupSlotError, match="not reachable from the target VM's node"):
        await slots.restore_from_slot(db_session, world.slot1, world.vm2, confirm_data_loss=True)
    world.pbs.storages = []
    with pytest.raises(BackupSlotError, match="not reachable"):
        await slots.restore_from_slot(db_session, world.slot1, world.vm2, confirm_data_loss=True)


@pytest.mark.asyncio
async def test_restore_refused_for_guest_agent_target_or_backup(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    agent_vm = world.vm(world.users["alice"], "agent-vm", 5102, strategy="guest_agent")
    with pytest.raises(BackupSlotError, match="a cloud-init Linux VM but 'agent-vm' is a guest-agent Linux VM"):
        await slots.restore_from_slot(db_session, world.slot1, agent_vm, confirm_data_loss=True)


async def _retain_macos_backup(world, db_session, vmid=5300, name="mac-a"):
    mac_source = world.vm(world.users["alice"], name, vmid, strategy="macos_guest_agent")
    volid = world.pbs.add(vmid, "a", notes="rf1:tpl=macos-14;svc=1")
    await slots.retain_from_vm(db_session, world.slot1, mac_source, volid=volid)
    return mac_source, volid


@pytest.mark.asyncio
async def test_macos_backup_restores_onto_another_macos_vm_with_the_macos_job(world, db_session):
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO

    mac_source, volid = await _retain_macos_backup(world, db_session)
    mac_target = world.vm(world.users["alice"], "mac-b", 5301, strategy="macos_guest_agent")
    with pytest.raises(BackupSlotError, match="replaces everything on that VM") as err:
        await slots.restore_from_slot(db_session, world.slot1, mac_target)
    assert "Apple account sign-ins" in err.value.message
    out = await slots.restore_from_slot(db_session, world.slot1, mac_target, confirm_data_loss=True, start=False)
    assert out["status"] == "queued" and out["start"] is True
    state = _reload(db_session, mac_target).config["vm_restore"]
    assert state["source_service_id"] == mac_source.id and state["start"] is True
    job = VMDeploymentJobDAO.get_by_id(db_session, out["job_id"])
    assert job.strategy_name == "restore_retained_backup_macos"


@pytest.mark.asyncio
async def test_macos_backup_is_refused_onto_a_cloudinit_or_windows_vm(world, db_session):
    await _retain_macos_backup(world, db_session)
    with pytest.raises(BackupSlotError, match="a macOS VM but 'vm-two' is a cloud-init Linux VM"):
        await slots.restore_from_slot(db_session, world.slot1, world.vm2, confirm_data_loss=True)
    win_vm = world.vm(world.users["alice"], "win-vm", 5302, strategy="windows_guest_agent")
    with pytest.raises(BackupSlotError, match="a macOS VM but 'win-vm' is a Windows VM"):
        await slots.restore_from_slot(db_session, world.slot1, win_vm, confirm_data_loss=True)


@pytest.mark.asyncio
async def test_cloudinit_backup_is_refused_onto_a_macos_vm(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    mac_target = world.vm(world.users["alice"], "mac-b", 5301, strategy="macos_guest_agent")
    with pytest.raises(BackupSlotError, match="a cloud-init Linux VM but 'mac-b' is a macOS VM") as err:
        await slots.restore_from_slot(db_session, world.slot1, mac_target, confirm_data_loss=True)
    assert err.value.status_code == 409
    assert world.pbs.restore_calls == []


@pytest.mark.asyncio
async def test_windows_backup_is_still_refused_onto_another_vm(world, db_session):
    win_source = world.vm(world.users["alice"], "win-a", 5303, strategy="windows_guest_agent")
    volid = world.pbs.add(5303, "a", notes="rf1:tpl=win-2022;svc=1")
    await slots.retain_from_vm(db_session, world.slot1, win_source, volid=volid)
    win_target = world.vm(world.users["alice"], "win-b", 5304, strategy="windows_guest_agent")
    with pytest.raises(BackupSlotError, match="a Windows VM, which can only be restored onto the VM it came from"):
        await slots.restore_from_slot(db_session, world.slot1, win_target, confirm_data_loss=True)


@pytest.mark.asyncio
async def test_macos_backup_onto_the_same_vm_takes_the_ordinary_restore(world, db_session):
    mac_source, volid = await _retain_macos_backup(world, db_session)
    retained = RetainedBackupDAO.get_by_volid(db_session, volid)
    with patch(
        "app.services.vm_backup_service.enqueue_restore_backup_job", return_value={"job_id": 9, "async": True}
    ) as enqueue:
        out = await restore_retained_onto(db_session, retained, mac_source)
    assert out["job_id"] == 9
    assert enqueue.call_args.kwargs["volid"] == volid


@pytest.mark.asyncio
async def test_running_macos_restore_blocks_purging_the_backup(world, db_session):
    _mac_source, volid = await _retain_macos_backup(world, db_session)
    mac_target = world.vm(world.users["alice"], "mac-b", 5301, strategy="macos_guest_agent")
    await slots.restore_from_slot(db_session, world.slot1, mac_target, confirm_data_loss=True)
    retained = RetainedBackupDAO.get_by_volid(db_session, volid)
    assert slots._restore_in_progress(db_session, retained.id) is True


@pytest.mark.asyncio
async def test_macos_restore_retry_needs_a_failed_restore_and_queues_the_retry_job(world, db_session):
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
    from app.models.vm_deployment_job import DeploymentJobStatus
    from app.services.retained_restore_service import enqueue_macos_restore_retry

    await _retain_macos_backup(world, db_session)
    mac_target = world.vm(world.users["alice"], "mac-b", 5301, strategy="macos_guest_agent")
    out = await slots.restore_from_slot(db_session, world.slot1, mac_target, confirm_data_loss=True)
    with pytest.raises(BackupSlotError, match="no macOS restore waiting"):
        enqueue_macos_restore_retry(db_session, mac_target)

    first = VMDeploymentJobDAO.get_by_id(db_session, out["job_id"])
    first.status = DeploymentJobStatus.FAILED
    state = dict(mac_target.config["vm_restore"])
    state.update({"status": "failed", "error": "boom", "identity_reapplied": True, "configure_attempts": 3})
    mac_target.config = {**mac_target.config, "vm_restore": state}
    db_session.commit()

    retry = enqueue_macos_restore_retry(db_session, mac_target)
    job = VMDeploymentJobDAO.get_by_id(db_session, retry["job_id"])
    assert job.strategy_name == "retry_restore_macos_configuration"
    state = _reload(db_session, mac_target).config["vm_restore"]
    assert state["status"] == "restored" and "error" not in state and "configure_attempts" not in state
    assert state["identity_reapplied"] is True and state["job_id"] == retry["job_id"]


@pytest.mark.asyncio
async def test_restore_refused_when_slot_suspended_or_target_inactive(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    world.vm2.status = ServiceStatus.SUSPENDED
    db_session.commit()
    with pytest.raises(BackupSlotError, match="only an active VM"):
        await slots.restore_from_slot(db_session, world.slot1, world.vm2, confirm_data_loss=True)
    world.vm2.status = ServiceStatus.ACTIVE
    world.slot1.status = ServiceStatus.SUSPENDED
    db_session.commit()
    with pytest.raises(BackupSlotError, match="suspended"):
        await slots.restore_from_slot(db_session, world.slot1, world.vm2, confirm_data_loss=True)
    # Staff may restore from a suspended slot.
    out = await slots.restore_from_slot(db_session, world.slot1, world.vm2, confirm_data_loss=True, staff=True)
    assert out["status"] == "queued"


@pytest.mark.asyncio
async def test_restore_onto_same_vm_uses_the_ordinary_restore_without_confirmation(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    retained = RetainedBackupDAO.get_by_volid(db_session, volid)
    with patch(
        "app.services.vm_backup_service.enqueue_restore_backup_job", return_value={"job_id": 7, "async": True}
    ) as enqueue:
        out = await restore_retained_onto(db_session, retained, world.vm1)
    assert out["job_id"] == 7
    assert enqueue.call_args.kwargs["volid"] == volid


@pytest.mark.asyncio
async def test_vm_restore_route_dispatches_retained_volids(world, db_session):
    from app.services.vm_backup_service import restore_service_backup

    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    with pytest.raises(BackupSlotError, match="confirm_data_loss"):
        await restore_service_backup(db_session, world.vm2, volid=volid, storage=STORAGE)
    out = await restore_service_backup(
        db_session, world.vm2, volid=volid.split(":", 1)[1], storage=STORAGE, confirm_data_loss=True
    )
    assert out["kind"] == "retained"


# --- macOS restore: failure recording, retry gate, power guard ---


async def _queued_macos_restore(world, db_session):
    await _retain_macos_backup(world, db_session)
    target = world.vm(world.users["alice"], "mac-b", 5301, strategy="macos_guest_agent")
    out = await slots.restore_from_slot(db_session, world.slot1, target, confirm_data_loss=True)
    return target, out["job_id"]


def _set_restore_state(db, service, **changes):
    state = {**dict(service.config["vm_restore"]), **changes}
    service.config = {**service.config, "vm_restore": state}
    db.commit()


def _fail_job_for_real(db, job_id, message="step failed"):
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
    from app.services.deployment import runner

    job = VMDeploymentJobDAO.get_by_id(db, job_id)
    with patch("app.services.client_notifications.notify_job_finished"):
        runner._fail_job(db, job, message)
    db.expire_all()


@pytest.mark.asyncio
async def test_macos_job_failing_for_any_reason_records_a_failed_restore_and_errors_the_vm(world, db_session):
    from app.models.service_vm import VMGuestState
    from app.services.retained_restore_service import enqueue_macos_restore_retry

    target, job_id = await _queued_macos_restore(world, db_session)
    _set_restore_state(
        db_session, target, status="restored", identity_reapplied=True, link_held=True, upid="UPID:restore"
    )
    _fail_job_for_real(db_session, job_id, "Exceeded max reclaim attempts (2)")
    target = _reload(db_session, target)
    state = target.config["vm_restore"]
    assert state["status"] == "failed" and state["error"] == "Exceeded max reclaim attempts (2)"
    assert state["link_held"] is True
    assert target.vm.guest_state == VMGuestState.ERROR
    assert target.vm.guest_last_error == "Exceeded max reclaim attempts (2)"
    assert enqueue_macos_restore_retry(db_session, target)["status"] == "queued"


@pytest.mark.asyncio
async def test_retry_is_accepted_whatever_state_a_dead_job_left_behind(world, db_session):
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
    from app.models.vm_deployment_job import DeploymentJobStatus
    from app.services.retained_restore_service import enqueue_macos_restore_retry

    target, job_id = await _queued_macos_restore(world, db_session)
    _set_restore_state(db_session, target, status="running", identity_reapplied=True, link_held=True)
    VMDeploymentJobDAO.get_by_id(db_session, job_id).status = DeploymentJobStatus.FAILED
    db_session.commit()
    assert enqueue_macos_restore_retry(db_session, _reload(db_session, target))["status"] == "queued"


@pytest.mark.asyncio
async def test_retry_is_refused_while_a_job_is_active_or_the_restore_is_finished_or_never_got_far(world, db_session):
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
    from app.models.vm_deployment_job import DeploymentJobStatus
    from app.services.retained_restore_service import enqueue_macos_restore_retry

    target, job_id = await _queued_macos_restore(world, db_session)
    _set_restore_state(db_session, target, status="failed", error="x", identity_reapplied=True)
    with pytest.raises(BackupSlotError, match="Another deployment job is already active") as err:
        enqueue_macos_restore_retry(db_session, target)
    assert err.value.status_code == 409

    VMDeploymentJobDAO.get_by_id(db_session, job_id).status = DeploymentJobStatus.FAILED
    db_session.commit()
    _set_restore_state(db_session, target, status="success")
    with pytest.raises(BackupSlotError, match="no macOS restore waiting"):
        enqueue_macos_restore_retry(db_session, target)
    _set_restore_state(db_session, target, status="failed", identity_reapplied=False)
    with pytest.raises(BackupSlotError, match="no macOS restore waiting"):
        enqueue_macos_restore_retry(db_session, target)


@pytest.mark.asyncio
async def test_failed_retry_enqueue_leaves_the_failed_state_untouched(world, db_session):
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
    from app.models.service_vm import VMGuestState
    from app.models.vm_deployment_job import DeploymentJobStatus
    from app.services.retained_restore_service import enqueue_macos_restore_retry

    target, job_id = await _queued_macos_restore(world, db_session)
    VMDeploymentJobDAO.get_by_id(db_session, job_id).status = DeploymentJobStatus.FAILED
    _set_restore_state(
        db_session, target, status="failed", error="boom", identity_reapplied=True, configure_attempts=3
    )
    target.vm.guest_state = VMGuestState.ERROR
    target.vm.guest_last_error = "boom"
    db_session.commit()
    before = dict(_reload(db_session, target).config["vm_restore"])

    with patch.object(VMDeploymentJobDAO, "create_job", side_effect=RuntimeError("database is down")):
        with pytest.raises(RuntimeError, match="database is down"):
            enqueue_macos_restore_retry(db_session, target)
    target = _reload(db_session, target)
    assert target.config["vm_restore"] == before
    assert target.vm.guest_state == VMGuestState.ERROR and target.vm.guest_last_error == "boom"
    assert enqueue_macos_restore_retry(db_session, target)["status"] == "queued"


@pytest.mark.asyncio
async def test_retry_action_is_for_staff_only_and_queues_the_retry_job(world, db_session):
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
    from app.models.vm_deployment_job import DeploymentJobStatus
    from app.services.strategy_actions import StrategyActionError, list_actions, run_action

    target, job_id = await _queued_macos_restore(world, db_session)
    VMDeploymentJobDAO.get_by_id(db_session, job_id).status = DeploymentJobStatus.FAILED
    _set_restore_state(db_session, target, status="failed", error="boom", identity_reapplied=True)

    assert "retry_restore_configuration" not in {a["name"] for a in list_actions(db_session, target, "client")}
    assert "retry_restore_configuration" in {a["name"] for a in list_actions(db_session, target, "admin")}
    with pytest.raises(StrategyActionError) as refused:
        await run_action(db_session, target, "retry_restore_configuration", {}, "client")
    assert refused.value.status_code == 403
    assert _reload(db_session, target).config["vm_restore"]["status"] == "failed"

    result = await run_action(db_session, target, "retry_restore_configuration", {}, "admin")
    assert result["action"] == "retry_restore_configuration" and result["status"] == "queued"
    job = VMDeploymentJobDAO.get_by_id(db_session, result["job_id"])
    assert job.strategy_name == "retry_restore_macos_configuration"


@pytest.mark.asyncio
async def test_retry_action_reports_a_refusal_as_a_conflict(world, db_session):
    from app.services.strategy_actions import StrategyActionError, run_action

    target, _job_id = await _queued_macos_restore(world, db_session)
    with pytest.raises(StrategyActionError, match="no macOS restore waiting") as err:
        await run_action(db_session, target, "retry_restore_configuration", {}, "admin")
    assert err.value.status_code == 409


@pytest.mark.asyncio
async def test_vm_cannot_be_started_between_the_restore_and_its_identity_being_put_back(world, db_session):
    from fastapi import HTTPException

    from app.api.services_client import _validate_client_power_action
    from app.services.retained_restore_service import power_block_reason

    target, job_id = await _queued_macos_restore(world, db_session)
    # Before the restore task has run, the VM is still the target's own.
    assert power_block_reason(target, "on") is None
    _set_restore_state(db_session, target, status="running", upid="UPID:restore")
    for action in ("on", "reboot", "reset"):
        assert "has not finished putting the VM's own network identity back" in power_block_reason(target, action)
    assert power_block_reason(target, "off") is None
    with pytest.raises(HTTPException) as err:
        _validate_client_power_action(target, "on", None)
    assert err.value.status_code == 409

    # The job failing leaves it blocked: the source's identity is still what Proxmox holds.
    _fail_job_for_real(db_session, job_id, "Restore task failed")
    failed = _reload(db_session, target)
    assert failed.config["vm_restore"]["status"] == "failed"
    assert power_block_reason(failed, "on") is not None

    # Once the target's identity is back the VM may be started again, held link or not.
    _set_restore_state(db_session, failed, identity_reapplied=True, link_held=True)
    assert power_block_reason(_reload(db_session, target), "on") is None


@pytest.mark.asyncio
async def test_restoring_again_after_an_interrupted_restore_keeps_the_targets_own_identity(world, db_session):
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
    from app.models.vm_deployment_job import DeploymentJobStatus

    target, job_id = await _queued_macos_restore(world, db_session)
    own = {"nets": {"net0": "e1000=AA:BB:CC:00:00:02,bridge=vmbr0"}, "name": "mac-b", "config": {"smbios1": "x"}}
    _set_restore_state(db_session, target, status="running", upid="UPID:restore", target_identity=own)
    _fail_job_for_real(db_session, job_id, "Restore task failed")
    assert VMDeploymentJobDAO.get_by_id(db_session, job_id).status == DeploymentJobStatus.FAILED

    await slots.restore_from_slot(db_session, world.slot1, _reload(db_session, target), confirm_data_loss=True)
    state = _reload(db_session, target).config["vm_restore"]
    assert state["target_identity"] == own and state["carried_identity"] is True
    assert "upid" not in state and state["status"] == "queued"


@pytest.mark.asyncio
async def test_restoring_after_a_clean_failure_captures_the_identity_afresh(world, db_session):
    target, job_id = await _queued_macos_restore(world, db_session)
    _set_restore_state(db_session, target, status="queued", target_identity={"nets": {}, "name": "mac-b"})
    _fail_job_for_real(db_session, job_id, "Could not stop the guest")
    await slots.restore_from_slot(db_session, world.slot1, _reload(db_session, target), confirm_data_loss=True)
    state = _reload(db_session, target).config["vm_restore"]
    assert "target_identity" not in state and "carried_identity" not in state


@pytest.mark.asyncio
async def test_cloudinit_restore_state_carries_no_macos_markers(world, db_session):
    volid = world.pbs.add(5100, "a")
    await slots.retain_from_vm(db_session, world.slot1, world.vm1, volid=volid)
    await slots.restore_from_slot(db_session, world.slot1, world.vm2, confirm_data_loss=True)
    state = _reload(db_session, world.vm2).config["vm_restore"]
    assert "macos" not in state and "carried_identity" not in state


# --- guest type matching, one case per mismatch ---


@pytest.mark.parametrize(
    ("backup_os", "target_strategy", "message"),
    [
        ("Linux - Cloudinit", "macos_guest_agent", "a cloud-init Linux VM but 'tgt' is a macOS VM"),
        ("Linux - Cloudinit", "windows_guest_agent", "a cloud-init Linux VM but 'tgt' is a Windows VM"),
        ("Linux - Cloudinit", "guest_agent", "a cloud-init Linux VM but 'tgt' is a guest-agent Linux VM"),
        ("macOS - Guest agent", "cloudinit_clone", "a macOS VM but 'tgt' is a cloud-init Linux VM"),
        ("macOS - Guest agent", "windows_guest_agent", "a macOS VM but 'tgt' is a Windows VM"),
        ("macOS - Guest agent", "guest_agent", "a macOS VM but 'tgt' is a guest-agent Linux VM"),
        ("macOS - Guest agent", None, "a macOS VM but 'tgt' is an unidentified VM"),
        ("Windows - Guest agent", "windows_guest_agent", "a Windows VM, which can only be restored onto the VM it came from"),
        ("Windows - Guest agent", "macos_guest_agent", "a Windows VM, which can only be restored onto the VM it came from"),
        ("Linux - Guest agent", "guest_agent", "a guest-agent Linux VM, which can only be restored onto the VM it came from"),
        (None, "macos_guest_agent", "an unidentified VM, which can only be restored onto the VM it came from"),
    ],
)
def test_guest_type_mismatches_are_refused_with_a_message_naming_both_types(backup_os, target_strategy, message):
    from types import SimpleNamespace

    from app.services.retained_restore_service import _check_guest_types_match

    target = SimpleNamespace(
        name="tgt", config={"vm_plan": {"strategy_name": target_strategy}} if target_strategy else {}
    )
    with pytest.raises(BackupSlotError, match=message) as err:
        _check_guest_types_match(target, SimpleNamespace(os_type=backup_os))
    assert err.value.status_code == 409


@pytest.mark.parametrize(
    ("backup_os", "target_strategy"),
    [("Linux - Cloudinit", "cloudinit_clone"), ("macOS - Guest agent", "macos_guest_agent")],
)
def test_matching_restorable_guest_types_are_accepted(backup_os, target_strategy):
    from types import SimpleNamespace

    from app.services.retained_restore_service import _check_guest_types_match

    target = SimpleNamespace(name="tgt", config={"vm_plan": {"strategy_name": target_strategy}})
    _check_guest_types_match(target, SimpleNamespace(os_type=backup_os))
