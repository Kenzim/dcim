"""Restore a retained backup onto one of the client's VMs (restore, clone or move).

The restore runs as the durable ``restore_retained_backup`` deployment job (``restore_retained_backup_macos``
for macOS), which overwrites the target's disks, then puts the target's own identity back (network
interfaces and MAC addresses, name, size, address, credentials; for macOS also its SMBIOS identity, through
the guest agent). Everything is checked before the job is queued so a refusal never leaves a VM half restored.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.backup_slot import RetainedBackup
from app.models.service import Service, ServiceStatus, ServiceType
from app.services.backup_slot_service import (
    GIB,
    BackupSlotError,
    iso_utc,
    slot_state,
)

logger = logging.getLogger(__name__)

RESTORE_RETAINED_STRATEGY = "restore_retained_backup"
RESTORE_RETAINED_MACOS_STRATEGY = "restore_retained_backup_macos"
RESTORE_RETAINED_STRATEGIES = (RESTORE_RETAINED_STRATEGY, RESTORE_RETAINED_MACOS_STRATEGY)
RETRY_RESTORE_MACOS_STRATEGY = "retry_restore_macos_configuration"
_CLOUDINIT_STRATEGY = "cloudinit_clone"
_MACOS_STRATEGY = "macos_guest_agent"
# Guest types that can be restored onto another VM, and the job that does it. Cloud-init guests get their
# address and credentials back through cloud-init; macOS guests through the guest agent after boot. Other
# guest-agent guests (Windows, Linux) are not covered: a restored Windows guest would also carry the source's
# computer name and machine SID, which the guest-agent set-up does not change.
_RESTORE_JOBS = {
    _CLOUDINIT_STRATEGY: RESTORE_RETAINED_STRATEGY,
    _MACOS_STRATEGY: RESTORE_RETAINED_MACOS_STRATEGY,
}
_GUEST_LABELS = {
    _CLOUDINIT_STRATEGY: "a cloud-init Linux",
    _MACOS_STRATEGY: "a macOS",
    "guest_agent": "a guest-agent Linux",
    "windows_guest_agent": "a Windows",
}


_MACOS_NOTE = (
    "Apple account sign-ins inside macOS may need redoing, because they are tied to the VM's serial number. "
)


def data_loss_message(target: Service, retained: RetainedBackup) -> str:
    label = retained.name or retained.source_service_name or f"backup {retained.id}"
    return (
        f"Restoring '{label}' onto '{target.name}' replaces everything on that VM: its disks, files and installed "
        "system are lost and cannot be recovered. Its network address, size and login credentials stay as they are. "
        f"{_MACOS_NOTE if _target_strategy_name(target) == _MACOS_STRATEGY else ''}"
        "Send confirm_data_loss=true to go ahead."
    )


def _backup_strategy_name(retained: RetainedBackup) -> Optional[str]:
    from app.services.vm_install_type_strategy import resolve_vm_template_strategy

    if not retained.os_type:
        return None
    try:
        return resolve_vm_template_strategy(retained.os_type)["strategy_name"]
    except ValueError:
        return None


def _target_strategy_name(target: Service) -> Optional[str]:
    plan = (target.config or {}).get("vm_plan") if isinstance(target.config, dict) else None
    return plan.get("strategy_name") if isinstance(plan, dict) else None


def _check_target(target: Service, retained: RetainedBackup, *, staff: bool) -> None:
    slot = retained.slot
    slot_service = slot.service if slot else None
    if slot is None or slot_service is None:
        raise BackupSlotError("The slot holding this backup no longer exists", 409)
    state = slot_state(slot_service, slot)
    if state == "terminated" and not staff:
        raise BackupSlotError("The backup slot is terminated", 409)
    if state == "suspended" and not staff:
        raise BackupSlotError("The backup slot is suspended", 409)
    if target.service_type != ServiceType.VM:
        raise BackupSlotError("A retained backup can only be restored onto a VM")
    if target.owner_user_id is None or target.owner_user_id != retained.owner_user_id:
        raise BackupSlotError("The target VM must belong to the same client as the backup", 403)
    if target.status != ServiceStatus.ACTIVE:
        raise BackupSlotError(f"The target VM is {target.status.value}; only an active VM can be restored onto", 409)
    if retained.resource_type != "vm":
        raise BackupSlotError("This retained backup is not a VM backup")
    if retained.source_service_id != target.id:
        _check_guest_types_match(target, retained)


def _guest_label(strategy_name: Optional[str]) -> str:
    return _GUEST_LABELS.get(strategy_name or "", "an unidentified")


def _check_guest_types_match(target: Service, retained: RetainedBackup) -> None:
    backup_strategy = _backup_strategy_name(retained)
    target_strategy = _target_strategy_name(target)
    if backup_strategy not in _RESTORE_JOBS:
        raise BackupSlotError(
            f"This backup is of {_guest_label(backup_strategy)} VM, which can only be restored onto the VM it "
            "came from. Only cloud-init Linux and macOS backups can be restored onto another VM.",
            409,
        )
    if target_strategy != backup_strategy:
        raise BackupSlotError(
            f"This backup is of {_guest_label(backup_strategy)} VM but '{target.name}' is "
            f"{_guest_label(target_strategy)} VM. A backup can only be restored onto the same type of VM.",
            409,
        )


def _restore_job_name(target: Service) -> str:
    return _RESTORE_JOBS.get(_target_strategy_name(target) or "", RESTORE_RETAINED_STRATEGY)


async def _check_placement(db: Session, target: Service, retained: RetainedBackup) -> Any:
    from app.services.vm_backup_service import BackupConfigError, get_vm_backup_plugin

    try:
        plugin, cluster_id, _node, _vmid = await get_vm_backup_plugin(db, target)
    except BackupConfigError as exc:
        raise BackupSlotError(str(exc), 409) from exc
    if retained.proxmox_cluster_id is not None and retained.proxmox_cluster_id != cluster_id:
        raise BackupSlotError("The target VM is in a different Proxmox cluster from the backup", 409)

    try:
        storages = await plugin.list_node_storages(content="backup")
    except Exception as exc:
        raise BackupSlotError(f"Could not check the backup storage on the target's node: {exc}", 502) from exc
    if not any(s.get("storage") == retained.storage and s.get("active") for s in storages):
        raise BackupSlotError(
            f"The backup storage '{retained.storage}' is not reachable from the target VM's node", 409
        )
    return plugin


async def _check_disk(plugin, retained: RetainedBackup) -> None:
    from app.services.deployment.disk_sizing import select_primary_disk

    if not retained.disk_bytes:
        return
    try:
        selected = select_primary_disk(await plugin.get_qemu_config())
    except Exception as exc:
        raise BackupSlotError(f"Could not read the target VM's disk size: {exc}", 502) from exc
    if selected is None:
        raise BackupSlotError("The target VM has no disk to restore onto", 409)
    target_bytes = int(selected[1] * GIB)
    if target_bytes < retained.disk_bytes:
        raise BackupSlotError(
            f"The target VM's disk is {target_bytes // GIB} GB but the backup's disk is "
            f"{-(-retained.disk_bytes // GIB)} GB; choose a VM with a disk at least as large",
            409,
        )


def _enqueue(db: Session, target: Service, retained: RetainedBackup, *, start: bool) -> dict[str, Any]:
    from app.dao.service_dao import ServiceDAO
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
    from app.models.service_vm import VMGuestState
    from app.services.deployment.registry import get_deployment_strategy_registry
    from app.services.vm_backup_service import BackupConfigError

    existing = VMDeploymentJobDAO.get_active_for_service(db, target.id)
    if existing is not None:
        raise BackupConfigError(
            f"Another deployment job is already active on this VM (job {existing.id}, "
            f"{existing.strategy_name}). Wait for it to finish before restoring."
        )
    strategy = get_deployment_strategy_registry().get(_restore_job_name(target))
    macos = strategy.name == RESTORE_RETAINED_MACOS_STRATEGY
    # A macOS guest has to boot for its own address to be set, so it always starts.
    start = True if macos else bool(start)
    state: dict[str, Any] = {
        "volid": retained.volid,
        "storage": retained.storage,
        "kind": "retained",
        "retained_backup_id": retained.id,
        "source_service_id": retained.source_service_id,
        "start": start,
        "vm_template_id": retained.vm_template_id,
        "notes": retained.notes or retained.rf_token,
        "status": "queued",
        "metadata_stale": False,
    }
    target.config = {**dict(target.config or {}), "vm_restore": state}
    if target.vm:
        target.vm.guest_state = VMGuestState.PROVISIONING
        target.vm.guest_last_error = None
    ServiceDAO.update(db, target)
    job = VMDeploymentJobDAO.create_job(
        db,
        service_id=target.id,
        strategy_name=strategy.name,
        step_names=strategy.step_names(),
        max_attempts=strategy.max_attempts,
    )
    target.config = {**dict(target.config or {}), "vm_restore": {**state, "job_id": job.id}}
    ServiceDAO.update(db, target)
    logger.info(
        "Enqueued %s job %s: retained backup %s onto service %s",
        strategy.name,
        job.id,
        retained.id,
        target.id,
    )
    return {
        "status": "queued",
        "job_id": job.id,
        "volid": retained.volid,
        "kind": "retained",
        "retained_backup_id": retained.id,
        "target_service_id": target.id,
        "start": start,
        "async": True,
        "waited": False,
    }


def enqueue_macos_restore_retry(db: Session, target: Service) -> dict[str, Any]:
    """Queue the guest configuration of a macOS restore that stopped before it finished.

    The VM keeps the restored disk; the job boots it, waits for the guest agent, applies the target's own
    network and password again, connects the public network interface and finishes the restore.
    """
    from app.dao.service_dao import ServiceDAO
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
    from app.models.service_vm import VMGuestState
    from app.services.deployment.registry import get_deployment_strategy_registry

    state = dict((target.config or {}).get("vm_restore") or {})
    if state.get("status") != "failed" or not state.get("identity_reapplied"):
        raise BackupSlotError(
            "There is no macOS restore waiting for its guest configuration on this VM", 409
        )
    existing = VMDeploymentJobDAO.get_active_for_service(db, target.id)
    if existing is not None:
        raise BackupSlotError(
            f"Another deployment job is already active on this VM (job {existing.id}, {existing.strategy_name})", 409
        )
    for key in ("error", "configure_attempts", "configure_error", "agent_wait_started"):
        state.pop(key, None)
    state["status"] = "restored"
    target.config = {**dict(target.config or {}), "vm_restore": state}
    if target.vm:
        target.vm.guest_state = VMGuestState.PROVISIONING
        target.vm.guest_last_error = None
    ServiceDAO.update(db, target)
    strategy = get_deployment_strategy_registry().get(RETRY_RESTORE_MACOS_STRATEGY)
    job = VMDeploymentJobDAO.create_job(
        db,
        service_id=target.id,
        strategy_name=strategy.name,
        step_names=strategy.step_names(),
        max_attempts=strategy.max_attempts,
    )
    target.config = {**dict(target.config or {}), "vm_restore": {**state, "job_id": job.id}}
    ServiceDAO.update(db, target)
    return {"status": "queued", "job_id": job.id, "target_service_id": target.id}


async def restore_retained_onto(
    db: Session,
    retained: RetainedBackup,
    target: Service,
    *,
    start: bool = True,
    confirm_data_loss: bool = False,
    staff: bool = False,
    vm_template_id: Optional[int] = None,
) -> dict[str, Any]:
    """Queue a restore of ``retained`` onto ``target``; refuses before anything is touched.

    Restoring onto the VM the backup came from takes the ordinary restore path.
    """
    _check_target(target, retained, staff=staff)

    if retained.source_service_id == target.id:
        from app.services.vm_backup_service import enqueue_restore_backup_job

        return enqueue_restore_backup_job(
            db, target, volid=retained.volid, storage=retained.storage, start=start, vm_template_id=vm_template_id
        )

    plugin = await _check_placement(db, target, retained)
    await _check_disk(plugin, retained)
    if not confirm_data_loss:
        raise BackupSlotError(data_loss_message(target, retained), 409)
    return _enqueue(db, target, retained, start=start)


def restore_targets(db: Session, retained: RetainedBackup) -> list[dict[str, Any]]:
    """The client's active VMs, for a "restore onto" picker (each is fully checked when queued)."""
    from app.dao.service_dao import ServiceDAO

    if retained.owner_user_id is None:
        return []
    return [
        {
            "service_id": service.id,
            "name": service.name,
            "is_source": service.id == retained.source_service_id,
            "backup_created_at": iso_utc(retained.backup_created_at),
        }
        for service in ServiceDAO.get_by_owner_user(db, retained.owner_user_id)
        if service.service_type == ServiceType.VM and service.status == ServiceStatus.ACTIVE
    ]
