"""Backup slots: keep a VM backup past the normal backup quota and VM termination.

One slot holds one retained backup up to the slot's size. Retaining sets
Proxmox's ``protected`` flag on the existing backup volume (no copy), so prune
jobs and ``purge_client_backups`` leave it alone. The retained backup belongs
to the client, so it survives the source VM and can be restored onto any of the
client's VMs (see ``retained_restore_service``).

Terminating the slot service starts a grace period (``backup_slot_destroy_grace_seconds``);
the backup is unprotected and deleted only after it. Suspending the slot blocks
new retains and restores but keeps the data.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao.backup_slot_dao import BackupSlotDAO, RetainedBackupDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.models.backup_slot import RESOURCE_VM, BackupSlot, RetainedBackup
from app.models.service import Service, ServiceStatus, ServiceType
from app.plugins.registry import get_registry
from app.services.proxmox_placement import cluster_to_proxmox_plugin_config

logger = logging.getLogger(__name__)

GIB = 1024**3
MAX_SIZE_GB = 102400
_MISSING_VOLUME = re.compile(r"does not exist|not found|no such", re.IGNORECASE)


class BackupSlotError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def destroy_grace() -> timedelta:
    return timedelta(seconds=max(0, int(settings.backup_slot_destroy_grace_seconds)))


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    value = _utc(value)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def gb_to_bytes(size_gb: int) -> int:
    return int(size_gb) * GIB


def check_size_gb(size_gb: Any, label: str = "Size") -> int:
    try:
        value = int(size_gb)
    except (TypeError, ValueError):
        raise BackupSlotError(f"{label} must be a whole number of GB") from None
    if value < 1 or value > MAX_SIZE_GB:
        raise BackupSlotError(f"{label} must be between 1 and {MAX_SIZE_GB} GB")
    return value


# --- state and payloads ---


def require_slot(db: Session, service: Service) -> BackupSlot:
    if service.service_type != ServiceType.BACKUP_SLOT:
        raise BackupSlotError("Not a backup slot service")
    slot = BackupSlotDAO.get_for_service(db, service.id)
    if slot is None:
        raise BackupSlotError("Backup slot record is missing for this service", 409)
    return slot


def slot_state(service: Service, slot: BackupSlot) -> str:
    if slot.terminated_at is not None or service.status == ServiceStatus.TERMINATED:
        return "terminated"
    if service.status == ServiceStatus.SUSPENDED:
        return "suspended"
    if service.status == ServiceStatus.ACTIVE:
        return "active"
    return service.status.value


def require_usable(service: Service, slot: BackupSlot) -> None:
    state = slot_state(service, slot)
    if state == "terminated":
        raise BackupSlotError("Backup slot is terminated", 409)
    if state == "suspended":
        raise BackupSlotError("Backup slot is suspended", 409)
    if state != "active":
        raise BackupSlotError(f"Backup slot is {state}", 409)


def retained_payload(row: RetainedBackup) -> dict[str, Any]:
    slot = row.slot
    return {
        "id": row.id,
        "slot_service_id": slot.service_id if slot else None,
        "resource_type": row.resource_type,
        "volid": row.volid,
        "storage": row.storage,
        "name": row.name,
        "notes": row.notes,
        "size_bytes": row.size_bytes,
        "disk_bytes": row.disk_bytes,
        "source_service_id": row.source_service_id,
        "source_service_name": row.source_service_name,
        "source_vmid": row.source_vmid,
        "template_code": row.template_code,
        "os_type": row.os_type,
        "backup_created_at": iso_utc(row.backup_created_at),
        "retained_at": iso_utc(row.created_at),
    }


def slot_summary(service: Service, slot: BackupSlot) -> dict[str, Any]:
    return {
        "service_id": service.id,
        "name": service.name,
        "status": slot_state(service, slot),
        "size_gb": slot.size_bytes // GIB,
        "size_bytes": slot.size_bytes,
        "occupied": slot.retained is not None,
        "retained": retained_payload(slot.retained) if slot.retained else None,
        "destroy_after": iso_utc(slot.destroy_after),
    }


def list_slot_summaries(db: Session, owner_user_id: int) -> list[dict[str, Any]]:
    out = []
    for slot in BackupSlotDAO.list_for_owner(db, owner_user_id):
        if slot.service is not None and slot.terminated_at is None:
            out.append(slot_summary(slot.service, slot))
    return out


def get_owned_retained(db: Session, retained_id: int, owner_user_id: int) -> RetainedBackup:
    """A retained backup of this client; anyone else's looks like it does not exist."""
    row = RetainedBackupDAO.get(db, retained_id)
    if row is None or row.owner_user_id != owner_user_id:
        raise BackupSlotError("Retained backup not found", 404)
    return row


# --- lifecycle ---


def resize(db: Session, service: Service, size_gb: int) -> BackupSlot:
    slot = require_slot(db, service)
    if slot.terminated_at is not None or service.status == ServiceStatus.TERMINATED:
        raise BackupSlotError("Backup slot is terminated", 409)
    new_bytes = gb_to_bytes(check_size_gb(size_gb))
    held = slot.retained.size_bytes if slot.retained else None
    if held and held > new_bytes:
        raise BackupSlotError(
            f"The retained backup is {held / GIB:.1f} GB; release it before shrinking the slot below that", 409
        )
    slot.size_bytes = new_bytes
    db.commit()
    db.refresh(slot)
    return slot


def mark_terminated(db: Session, service_id: int) -> None:
    """Start the grace period; the retained backup is deleted once it has passed."""
    slot = BackupSlotDAO.get_for_service(db, service_id)
    if slot is None or slot.terminated_at is not None:
        return
    now = _now()
    slot.terminated_at = now
    slot.destroy_after = now + destroy_grace() if slot.retained else now
    db.commit()


def mark_reinstated(db: Session, service_id: int) -> None:
    """Cancel a pending destruction when staff bring a terminated slot service back within the grace period."""
    slot = BackupSlotDAO.get_for_service(db, service_id)
    if slot is not None and slot.terminated_at is not None:
        slot.terminated_at = None
        slot.destroy_after = None
        db.commit()


def mark_terminated_quietly(db: Session, service_id: int) -> None:
    try:
        mark_terminated(db, service_id)
    except Exception:
        logger.exception("Backup slot: could not mark service %s terminated", service_id)
        db.rollback()


def reconcile_terminations(db: Session) -> int:
    """Mark slots whose service is terminated or gone; returns how many."""
    count = 0
    for slot in BackupSlotDAO.unterminated(db):
        service = slot.service
        if service is None or service.status == ServiceStatus.TERMINATED:
            mark_terminated(db, slot.service_id)
            count += 1
    return count


async def purge_expired(db: Session) -> int:
    """Delete retained backups of slots whose grace period has passed; returns slots removed."""
    removed = 0
    now = _now()
    for slot in BackupSlotDAO.terminated(db):
        due = _utc(slot.destroy_after)
        if due is None or due > now:
            continue
        if slot.retained is not None:
            try:
                await release_backup(db, slot.retained)
            except Exception:
                logger.exception("Backup slot %s: could not delete the retained backup yet", slot.id)
                db.rollback()
                continue
            db.refresh(slot)
        BackupSlotDAO.delete_slot(db, slot)
        removed += 1
    return removed


# --- Proxmox access ---


def get_cluster_plugin(
    db: Session, cluster_id: Optional[int], *, node: Optional[str] = None, vmid: Optional[int] = None
):
    """A Proxmox plugin for backup-storage calls that do not need a live VM."""
    if cluster_id is None:
        raise BackupSlotError("The retained backup has no Proxmox cluster recorded", 409)
    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if cluster is None:
        raise BackupSlotError(f"Proxmox cluster {cluster_id} no longer exists", 409)
    names = [str(n.node_name) for n in (cluster.nodes or []) if n.enabled and (n.node_name or "").strip()]
    node_name = node if node in names else (names[0] if names else (node or "").strip())
    if not node_name:
        raise BackupSlotError(f"Proxmox cluster {cluster_id} has no enabled node", 409)
    try:
        config = cluster_to_proxmox_plugin_config(cluster, node_name, int(vmid or 0))
    except ValueError as exc:
        raise BackupSlotError(str(exc), 409) from exc
    return get_registry().get_plugin("proxmox", config)


def _is_missing_volume(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 404:
        return True
    return bool(_MISSING_VOLUME.search(str(exc)))


async def _drop_volume(plugin, storage: str, volid: str) -> None:
    """Unprotect then delete a backup volume; a volume that is already gone counts as done."""
    for step in (
        lambda: plugin.set_backup_protected(storage, volid, False),
        lambda: plugin.delete_backup(storage, volid),
    ):
        try:
            await step()
        except Exception as exc:
            if not _is_missing_volume(exc):
                raise


def _parse_config_text(text: str) -> dict[str, str]:
    """Top section of a ``qemu-server.conf`` as key/value pairs."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if line.startswith("["):
            break
        key, sep, value = line.partition(":")
        if sep and key and not key.startswith("#"):
            out[key.strip()] = value.strip()
    return out


async def _backup_disk_bytes(plugin, volid: str) -> Optional[int]:
    from app.services.deployment.disk_sizing import select_primary_disk

    try:
        text = await plugin.extract_backup_config(volid)
    except Exception:
        return None
    selected = select_primary_disk(_parse_config_text(text))
    return int(selected[1] * GIB) if selected else None


# --- retain and release ---


async def retain_backup(
    db: Session,
    slot_service: Service,
    source_service: Service,
    *,
    volid: str,
    name: Optional[str] = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Keep one of ``source_service``'s client backups in the slot.

    The caller has checked that the client may use both services. ``replace``
    swaps out a backup the slot already holds, once the new one is protected.
    """
    from app.services.vm_backup_service import (
        BackupForbiddenError,
        _enrich_backup_identity,
        _resolve_backup_kind,
        get_vm_backup_plugin,
        list_running_backup_jobs,
        mark_running_client_backups,
        resolve_backup_settings,
    )
    from app.services.vm_identity_stamp import resolve_template_from_token

    slot = require_slot(db, slot_service)
    require_usable(slot_service, slot)
    if source_service.service_type != ServiceType.VM:
        raise BackupSlotError("Only VM backups can be retained")
    if source_service.owner_user_id is None or source_service.owner_user_id != slot_service.owner_user_id:
        raise BackupSlotError("The slot and the VM must belong to the same client", 403)
    if slot.retained is not None and not replace:
        raise BackupSlotError("This slot already holds a backup; release it or choose to replace it", 409)

    backup_settings = resolve_backup_settings(db, source_service)
    try:
        kind, normalized = _resolve_backup_kind(backup_settings, "", volid)
    except BackupForbiddenError as exc:
        raise BackupSlotError(str(exc), 403) from exc
    if kind != "client":
        raise BackupSlotError("Only client backups can be retained", 403)

    plugin, cluster_id, node, vmid = await get_vm_backup_plugin(db, source_service)
    rows = await plugin.list_backups(backup_settings.client_storage, vmid=vmid)
    row = next((r for r in rows if str(r.get("volid") or "") == normalized), None)
    if row is None:
        raise BackupSlotError("Backup not found for this VM", 404)
    if RetainedBackupDAO.get_by_volid(db, normalized) is not None:
        raise BackupSlotError("This backup is already retained", 409)

    items = [dict(r, kind="client", storage=backup_settings.client_storage) for r in rows]
    jobs = await list_running_backup_jobs(db, source_service)
    mark_running_client_backups(items, jobs, client_storage=backup_settings.client_storage)
    if any(i.get("running") and i.get("volid") == normalized for i in items):
        raise BackupSlotError("That backup is still being written; retain it once it has finished", 409)

    size = row.get("size")
    size_bytes = int(size) if size not in (None, "") else None
    if size_bytes is not None and size_bytes > slot.size_bytes:
        raise BackupSlotError(
            f"The backup is {size_bytes / GIB:.1f} GB but the slot holds at most {slot.size_bytes // GIB} GB", 409
        )

    item = dict(row)
    await _enrich_backup_identity(db, plugin, item, extract_cache={})
    template = resolve_template_from_token(db, item.get("rf_token"))
    disk_bytes = await _backup_disk_bytes(plugin, normalized)

    try:
        await plugin.set_backup_protected(backup_settings.client_storage, normalized, True)
    except Exception as exc:
        raise BackupSlotError(f"Could not protect the backup on the backup storage: {exc}", 502) from exc

    previous = slot.retained
    previous_ref = (previous.storage, previous.volid, previous.proxmox_cluster_id, previous.proxmox_node_name) if previous else None
    ctime = row.get("ctime")
    try:
        if previous is not None:
            slot.retained = None
            db.flush()
        retained = RetainedBackup(
            slot_id=slot.id,
            owner_user_id=slot_service.owner_user_id,
            resource_type=RESOURCE_VM,
            source_service_id=source_service.id,
            source_service_name=source_service.name,
            proxmox_cluster_id=cluster_id,
            proxmox_node_name=node,
            source_vmid=vmid,
            storage=backup_settings.client_storage,
            volid=normalized,
            name=(name or "").strip() or None,
            notes=row.get("notes"),
            size_bytes=size_bytes,
            disk_bytes=disk_bytes,
            vm_template_id=template.id if template else None,
            template_code=item.get("template_code"),
            os_type=item.get("os_type"),
            rf_token=item.get("rf_token"),
            backup_created_at=datetime.fromtimestamp(int(ctime), tz=timezone.utc) if ctime else None,
        )
        slot.retained = retained
        db.commit()
    except Exception:
        db.rollback()
        try:
            await plugin.set_backup_protected(backup_settings.client_storage, normalized, False)
        except Exception:
            logger.warning("Could not unprotect %s after a failed retain", normalized)
        raise

    replaced_cleanup_failed = False
    if previous_ref is not None:
        old_storage, old_volid, old_cluster, old_node = previous_ref
        try:
            old_plugin = get_cluster_plugin(db, old_cluster, node=old_node)
            await _drop_volume(old_plugin, old_storage, old_volid)
        except Exception:
            replaced_cleanup_failed = True
            logger.warning("Replaced retained backup %s could not be deleted from storage", old_volid, exc_info=True)

    db.refresh(retained)
    payload = retained_payload(retained)
    payload["replaced_cleanup_failed"] = replaced_cleanup_failed
    return payload


async def release_backup(db: Session, retained: RetainedBackup) -> None:
    """Unprotect and delete the retained backup, freeing its slot."""
    plugin = get_cluster_plugin(
        db, retained.proxmox_cluster_id, node=retained.proxmox_node_name, vmid=retained.source_vmid
    )
    try:
        await _drop_volume(plugin, retained.storage, retained.volid)
    except Exception as exc:
        raise BackupSlotError(f"Could not delete the backup from the backup storage: {exc}", 502) from exc
    RetainedBackupDAO.remove(db, retained)
    db.commit()


# --- actions shared by the client, admin, billing and MCP surfaces ---
# Callers have already authorised the services they pass in.


async def retain_from_vm(
    db: Session,
    slot_service: Service,
    source_service: Service,
    *,
    volid: str,
    name: Optional[str] = None,
    replace: bool = False,
) -> dict[str, Any]:
    from app.services.vm_backup_service import BackupConfigError, BackupForbiddenError

    try:
        return await retain_backup(
            db, slot_service, source_service, volid=volid, name=name, replace=replace
        )
    except (BackupConfigError, BackupForbiddenError) as exc:
        raise BackupSlotError(str(exc), 403 if isinstance(exc, BackupForbiddenError) else 409) from exc


async def release_from_slot(db: Session, slot_service: Service) -> dict[str, Any]:
    slot = require_slot(db, slot_service)
    if slot.retained is None:
        raise BackupSlotError("This slot holds no backup", 409)
    retained_id = slot.retained.id
    await release_backup(db, slot.retained)
    return {"released": True, "retained_backup_id": retained_id}


async def restore_from_slot(
    db: Session,
    slot_service: Service,
    target_service: Service,
    *,
    start: bool = True,
    confirm_data_loss: bool = False,
    staff: bool = False,
) -> dict[str, Any]:
    from app.services.retained_restore_service import restore_retained_onto
    from app.services.vm_backup_service import BackupConfigError, BackupForbiddenError

    slot = require_slot(db, slot_service)
    if slot.retained is None:
        raise BackupSlotError("This slot holds no backup", 409)
    try:
        return await restore_retained_onto(
            db, slot.retained, target_service, start=start, confirm_data_loss=confirm_data_loss, staff=staff
        )
    except (BackupConfigError, BackupForbiddenError) as exc:
        raise BackupSlotError(str(exc), 403 if isinstance(exc, BackupForbiddenError) else 409) from exc
