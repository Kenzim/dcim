"""Restore a retained backup onto one of the client's VMs (restore, clone or move).

The restore runs as the durable ``restore_retained_backup`` deployment job, which
overwrites the target's disks, then puts the target's own identity back (network
interfaces and MAC addresses, name, size, address, credentials). Everything is
checked before the job is queued so a refusal never leaves a VM half restored.
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
# Only guests configured through cloud-init can be restored onto another VM: their address and credentials
# are re-applied from the target's record. Guest-agent guests need their own set-up steps after a restore.
_CLOUDINIT_STRATEGY = "cloudinit_clone"


def data_loss_message(target: Service, retained: RetainedBackup) -> str:
    label = retained.name or retained.source_service_name or f"backup {retained.id}"
    return (
        f"Restoring '{label}' onto '{target.name}' replaces everything on that VM: its disks, files and installed "
        "system are lost and cannot be recovered. Its network address, size and login credentials stay as they are. "
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
        if _backup_strategy_name(retained) != _CLOUDINIT_STRATEGY:
            raise BackupSlotError(
                "This backup's guest type is not set up through cloud-init (or could not be identified), "
                "so it can only be restored onto the VM it came from",
                409,
            )
        if _target_strategy_name(target) != _CLOUDINIT_STRATEGY:
            raise BackupSlotError(
                "The target VM is not set up through cloud-init; restoring another VM's backup onto it is not supported",
                409,
            )


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
    strategy = get_deployment_strategy_registry().get(RESTORE_RETAINED_STRATEGY)
    state: dict[str, Any] = {
        "volid": retained.volid,
        "storage": retained.storage,
        "kind": "retained",
        "retained_backup_id": retained.id,
        "start": bool(start),
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
        RESTORE_RETAINED_STRATEGY,
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
        "start": bool(start),
        "async": True,
        "waited": False,
    }


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
