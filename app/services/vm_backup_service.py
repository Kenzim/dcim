"""VM backup orchestration against two Proxmox storages (platform vs client).

No RackFlow backup inventory table — PBS/PVE storage content is the catalog.
Product/family VM config supplies storage names and client quota.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.dao.product_catalog_dao import ProductDAO
from app.dao.vm_config_dao import ProductVMConfigDAO
from app.models.service import Service, ServiceType
from app.plugins.base import PowerState
from app.services.proxmox_placement import ProxmoxPlacementError, resolve_proxmox_plugin_for_service

logger = logging.getLogger(__name__)

DEFAULT_MAX_CLIENT_BACKUPS = 3


@dataclass(frozen=True)
class BackupSettings:
    platform_storage: str
    client_storage: str
    max_client_backups: int


class BackupConfigError(ValueError):
    """Raised when backup storages are missing or misconfigured."""


class BackupQuotaError(ValueError):
    """Raised when the client backup quota is exhausted."""


class BackupForbiddenError(ValueError):
    """Raised when a client tries to mutate a platform backup."""


def _specs_for_service(db: Session, service: Service) -> dict[str, Any]:
    snap = service.product_snapshot if isinstance(service.product_snapshot, dict) else {}
    specs = snap.get("effective_specs") if isinstance(snap.get("effective_specs"), dict) else {}
    if specs:
        return dict(specs)
    if not service.product_code:
        return {}
    product = ProductDAO.get_by_code(db, service.product_code)
    if not product:
        return {}
    return dict(ProductVMConfigDAO.resolve_effective_config(db, product) or {})


def resolve_backup_settings(db: Session, service: Service) -> BackupSettings:
    specs = _specs_for_service(db, service)
    platform = (specs.get("platform_backup_storage") or "").strip()
    client = (specs.get("client_backup_storage") or "").strip()
    if not platform or not client:
        raise BackupConfigError(
            "Backup storages are not configured on this product "
            "(platform_backup_storage / client_backup_storage)"
        )
    raw_max = specs.get("max_client_backups", DEFAULT_MAX_CLIENT_BACKUPS)
    try:
        max_client = int(raw_max)
    except (TypeError, ValueError) as exc:
        raise BackupConfigError("max_client_backups must be an integer") from exc
    if max_client < 0:
        raise BackupConfigError("max_client_backups must be >= 0")
    return BackupSettings(
        platform_storage=platform,
        client_storage=client,
        max_client_backups=max_client,
    )


async def get_vm_backup_plugin(db: Session, service: Service):
    """Resolve the live Proxmox plugin for a VM service's backups.

    Treats the cached node as a hint: if it's missing or stale (guest
    migrated), searches the cluster for the VMID's current node and updates
    the cache before erroring.
    """
    if service.service_type != ServiceType.VM:
        raise BackupConfigError("Not a VM service")
    try:
        plugin, cid, node, vmid = await resolve_proxmox_plugin_for_service(db, service)
    except ProxmoxPlacementError as exc:
        raise BackupConfigError(str(exc)) from exc
    return plugin, cid, node, vmid


def _normalize_volid(storage: str, volid: str) -> str:
    vol = str(volid or "").strip()
    if not vol:
        raise ValueError("volid is required")
    if ":" in vol:
        return vol
    return f"{storage}:{vol}"


def _item_payload(
    *,
    row: dict[str, Any],
    storage: str,
    kind: str,
    deletable: bool,
) -> dict[str, Any]:
    volid = row.get("volid")
    return {
        "volid": volid,
        "storage": storage,
        "kind": kind,
        "deletable": deletable,
        "vmid": row.get("vmid"),
        "ctime": row.get("ctime"),
        "size": row.get("size"),
        "format": row.get("format"),
        "notes": row.get("notes"),
        "subtype": row.get("subtype"),
    }


async def _backup_rf_token_from_extract(
    plugin,
    volid: str,
    extract_cache: dict[str, Optional[str]],
) -> Optional[str]:
    from app.services.vm_identity_stamp import (
        extract_smbios1_from_config_text,
        get_smbios1_sku,
        parse_rf_sku_token,
    )

    if not hasattr(plugin, "extract_backup_config"):
        return None
    if volid not in extract_cache:
        try:
            extract_cache[volid] = await plugin.extract_backup_config(str(volid))
        except Exception:
            extract_cache[volid] = None
    config_text = extract_cache.get(volid)
    smbios1 = extract_smbios1_from_config_text(config_text)
    parsed = parse_rf_sku_token(get_smbios1_sku(smbios1))
    return parsed.get("raw") if parsed else None


async def _enrich_backup_identity(
    db: Session,
    plugin,
    item: dict[str, Any],
    *,
    extract_cache: dict[str, Optional[str]],
) -> dict[str, Any]:
    """Attach template_code/name/os_type from PBS notes or extractconfig smbios1 sku."""
    from app.services.vm_identity_stamp import parse_rf_sku_token, resolve_template_from_token

    parsed = parse_rf_sku_token(item.get("notes"))
    token = parsed.get("raw") if parsed else None
    if not token:
        volid = item.get("volid")
        if volid:
            token = await _backup_rf_token_from_extract(plugin, str(volid), extract_cache)
            if token:
                parsed = parse_rf_sku_token(token)
    tmpl = resolve_template_from_token(db, token) if token else None
    item["rf_token"] = token
    item["template_code"] = tmpl.code if tmpl else (parsed.get("template_code") if parsed else None)
    item["template_name"] = tmpl.name if tmpl else None
    item["os_type"] = tmpl.os_type if tmpl else None
    return item


async def list_service_backups(db: Session, service: Service) -> List[dict[str, Any]]:
    settings = resolve_backup_settings(db, service)
    plugin, _cid, _node, vmid = await get_vm_backup_plugin(db, service)
    platform_rows = await plugin.list_backups(settings.platform_storage, vmid=vmid)
    client_rows = await plugin.list_backups(settings.client_storage, vmid=vmid)
    items = [
        _item_payload(row=r, storage=settings.platform_storage, kind="platform", deletable=False)
        for r in platform_rows
    ]
    items.extend(
        _item_payload(row=r, storage=settings.client_storage, kind="client", deletable=True)
        for r in client_rows
    )
    items.sort(key=lambda i: i.get("ctime") or 0, reverse=True)
    extract_cache: dict[str, Optional[str]] = {}
    for item in items:
        await _enrich_backup_identity(db, plugin, item, extract_cache=extract_cache)
    return items


_BACKUP_TASK_TYPES = frozenset({"vzdump", "qmrestore", "qrestore", "imgcopy"})


def _backup_task_matches_vmid(row: dict[str, Any], vmid: int) -> bool:
    row_vmid = row.get("vmid")
    try:
        if row_vmid is not None and int(row_vmid) != int(vmid):
            return False
    except (TypeError, ValueError):
        upid = str(row.get("upid") or "")
        if f":{vmid}:" not in upid and str(row_vmid) != str(vmid):
            return False
    return True


def _backup_job_from_task_row(
    row: dict[str, Any], *, vmid: int, node: str, client_storage: str
) -> dict[str, Any]:
    t = str(row.get("type") or "").lower()
    kind = "backup" if t == "vzdump" else "restore"
    storage = client_storage if kind == "backup" else None
    return {
        "upid": row.get("upid"),
        "type": t,
        "kind": kind,
        "scope": "client" if kind == "backup" else "restore",
        "status": row.get("status") or "RUNNING",
        "vmid": vmid,
        "node": row.get("node") or node,
        "starttime": row.get("starttime"),
        "storage": storage,
        "user": row.get("user"),
    }


async def _list_running_task_rows(plugin, vmid: int, service_id: int) -> list:
    try:
        return await plugin.list_tasks(running_only=True, vmid=vmid, limit=40)
    except Exception:
        try:
            return await plugin.list_tasks(running_only=True, limit=80)
        except Exception as exc:
            logger.warning("Failed to list Proxmox tasks for service %s: %s", service_id, exc)
            return []


async def list_running_backup_jobs(db: Session, service: Service) -> List[dict[str, Any]]:
    """Return active Proxmox tasks related to backup/restore for this VMID."""
    plugin, _cid, node, vmid = await get_vm_backup_plugin(db, service)
    try:
        client_storage = resolve_backup_settings(db, service).client_storage
    except BackupConfigError:
        client_storage = ""
    rows = await _list_running_task_rows(plugin, vmid, service.id)
    jobs: List[dict[str, Any]] = []
    for row in rows:
        t = str(row.get("type") or "").lower()
        if t not in _BACKUP_TASK_TYPES or not _backup_task_matches_vmid(row, vmid):
            continue
        jobs.append(_backup_job_from_task_row(row, vmid=vmid, node=node, client_storage=client_storage))
    return jobs


def _is_client_backup_row(row: dict[str, Any], client_storage: str) -> bool:
    if row.get("kind") != "client":
        return False
    store = (row.get("storage") or "").strip()
    return not client_storage or store == client_storage or store == ""


def _backup_job_start_times(jobs: List[dict[str, Any]]) -> List[int]:
    starts: List[int] = []
    for job in jobs:
        raw = job.get("starttime")
        if raw is None:
            continue
        try:
            starts.append(int(raw))
        except (TypeError, ValueError):
            continue
    return starts


def _mark_best_client_backup_for_start(
    backups: List[dict[str, Any]], start: int, client_storage: str
) -> bool:
    best: Optional[dict[str, Any]] = None
    best_delta: Optional[int] = None
    for row in backups:
        if not isinstance(row, dict) or not _is_client_backup_row(row, client_storage):
            continue
        raw_ctime = row.get("ctime")
        if raw_ctime is None:
            continue
        try:
            ctime = int(raw_ctime)
        except (TypeError, ValueError):
            continue
        if ctime < start - 120:
            continue
        delta = abs(ctime - start)
        if best is None or delta < best_delta:
            best = row
            best_delta = delta
    if best is None:
        return False
    best["running"] = True
    best["deletable"] = False
    return True


def _mark_first_client_backup_running(backups: List[dict[str, Any]], client_storage: str) -> None:
    for row in backups:
        if isinstance(row, dict) and _is_client_backup_row(row, client_storage):
            row["running"] = True
            row["deletable"] = False
            break


def mark_running_client_backups(
    backups: List[dict[str, Any]],
    jobs: List[dict[str, Any]],
    *,
    client_storage: str,
) -> None:
    """Flag client backups that PBS already lists while vzdump is still running.

    In-progress snapshots often appear in storage content before the task ends;
    mark those rows so UIs can show Running instead of Restore/Delete.
    """
    backup_jobs = [j for j in jobs if isinstance(j, dict) and j.get("kind") == "backup"]
    if not backup_jobs or not backups:
        return

    matched = any(
        _mark_best_client_backup_for_start(backups, start, client_storage)
        for start in _backup_job_start_times(backup_jobs)
    )
    if not matched:
        _mark_first_client_backup_running(backups, client_storage)


async def list_service_backups_and_jobs(
    db: Session, service: Service
) -> Tuple[List[dict[str, Any]], List[dict[str, Any]]]:
    """List backups + running jobs, marking in-flight client snapshots."""
    settings = resolve_backup_settings(db, service)
    items = await list_service_backups(db, service)
    jobs = await list_running_backup_jobs(db, service)
    mark_running_client_backups(items, jobs, client_storage=settings.client_storage)
    return items, jobs


async def create_client_backup(
    db: Session,
    service: Service,
    *,
    notes: Optional[str] = None,
    mode: str = "snapshot",
    wait: bool = False,
) -> dict[str, Any]:
    from app.dao.product_catalog_dao import VMTemplateDAO
    from app.services.vm_identity_stamp import build_rf_sku_token, notes_with_rf_token

    settings = resolve_backup_settings(db, service)
    plugin, _cid, _node, vmid = await get_vm_backup_plugin(db, service)

    running = await list_running_backup_jobs(db, service)
    if any(j.get("kind") == "backup" for j in running):
        raise BackupConfigError(
            "A backup is already running for this VM. Wait for it to finish."
        )

    existing = await plugin.list_backups(settings.client_storage, vmid=vmid)
    if len(existing) >= settings.max_client_backups:
        raise BackupQuotaError(
            f"Client backup quota reached ({settings.max_client_backups}). "
            "Delete an existing client backup first."
        )
    stamped_notes = notes
    if service.vm and service.vm.vm_template_id:
        tmpl = VMTemplateDAO.get_by_id(db, service.vm.vm_template_id)
        if tmpl and tmpl.code:
            stamped_notes = notes_with_rf_token(
                notes, build_rf_sku_token(service, template_code=tmpl.code)
            )
    upid = await plugin.create_backup(
        vmid,
        storage=settings.client_storage,
        notes=stamped_notes,
        mode=mode or "snapshot",
    )
    if wait:
        await plugin.wait_for_proxmox_task(upid, timeout=3600.0)
    return {
        "upid": upid,
        "storage": settings.client_storage,
        "vmid": vmid,
        "waited": wait,
        "status": "finished" if wait else "running",
    }


def _resolve_backup_kind(
    settings: BackupSettings, storage: str, volid: str
) -> Tuple[str, str]:
    """Return (kind, normalized_volid)."""
    store = (storage or "").strip()
    vol = _normalize_volid(store or settings.client_storage, volid)
    vol_store = vol.split(":", 1)[0] if ":" in vol else store
    if vol_store == settings.client_storage or store == settings.client_storage:
        return "client", vol
    if vol_store == settings.platform_storage or store == settings.platform_storage:
        return "platform", vol
    raise BackupForbiddenError("Backup storage is not configured for this service")


async def delete_client_backup(
    db: Session,
    service: Service,
    *,
    volid: str,
    storage: Optional[str] = None,
) -> None:
    settings = resolve_backup_settings(db, service)
    kind, normalized = _resolve_backup_kind(settings, storage or "", volid)
    if kind != "client":
        raise BackupForbiddenError("Platform backups cannot be deleted by clients")
    plugin, *_ = await get_vm_backup_plugin(db, service)
    await plugin.delete_backup(settings.client_storage, normalized)


RESTORE_BACKUP_STRATEGY = "restore_backup"


async def stop_guest_for_restore(plugin) -> None:
    """Stop a running guest so a force-restore can overwrite its VMID."""
    try:
        exists = await plugin.vm_exists()
    except Exception:
        exists = False
    if not exists:
        return
    try:
        state = await plugin.get_power_state()
    except Exception:
        state = PowerState.UNKNOWN
    if state != PowerState.ON:
        return
    await plugin.power_off(force=False)
    for _ in range(20):
        await asyncio.sleep(1.0)
        try:
            if await plugin.get_power_state() == PowerState.OFF:
                return
        except Exception:
            continue
    try:
        if await plugin.get_power_state() != PowerState.OFF:
            await plugin.power_off(force=True)
    except Exception:
        await plugin.power_off(force=True)


async def start_restore_task(
    db: Session,
    service: Service,
    *,
    volid: str,
    storage: Optional[str] = None,
    start: bool = False,
) -> dict[str, Any]:
    """Kick off a Proxmox restore (no wait). Caller must have stopped the guest."""
    settings = resolve_backup_settings(db, service)
    kind, normalized = _resolve_backup_kind(settings, storage or "", volid)
    plugin, _cid, _node, vmid = await get_vm_backup_plugin(db, service)
    upid = await plugin.restore_backup(
        archive=normalized,
        vmid=vmid,
        force=True,
        start=start,
    )
    return {
        "upid": upid,
        "storage": settings.client_storage if kind == "client" else settings.platform_storage,
        "kind": kind,
        "vmid": vmid,
        "volid": normalized,
    }


def apply_metadata_after_restore(
    db: Session,
    service: Service,
    *,
    vm_template_id: Optional[int] = None,
) -> None:
    """Refresh template/OS metadata after restore, or clear it when unknown.

    Cross-OS restores leave ``service.vm.vm_template_id`` / product_snapshot
    pointing at the previous guest. When the caller knows the backup's template,
    mirror the reinstall path; otherwise clear the stale identity.
    """
    from app.dao.service_dao import ServiceDAO
    from app.services.vm_ssh_keys_service import VmSshKeysError, apply_template_change_for_reinstall

    if vm_template_id is not None:
        try:
            apply_template_change_for_reinstall(
                db, service, vm_template_id=int(vm_template_id)
            )
            return
        except VmSshKeysError as exc:
            logger.warning(
                "Could not apply template %s after restore for service %s: %s",
                vm_template_id,
                service.id,
                exc,
            )

    # Unknown OS after restore: drop stale template identity so reinstall /
    # SSH-accept checks do not act on the previous guest.
    if service.vm and service.vm.vm_template_id is not None:
        service.vm.vm_template_id = None
    service.os_code = None
    cfg = dict(service.config or {})
    cfg.pop("product_snapshot", None)
    vm_plan = dict(cfg.get("vm_plan") or {})
    if vm_plan:
        vm_plan.pop("vm_template", None)
        vm_plan.pop("strategy_name", None)
        vm_plan.pop("strategy_plan", None)
        cfg["vm_plan"] = vm_plan
    restore = dict(cfg.get("vm_restore") or {})
    restore["metadata_stale"] = True
    cfg["vm_restore"] = restore
    service.config = cfg
    service.product_snapshot = None
    ServiceDAO.update(db, service)


def _existing_restore_job_response(
    existing, service: Service, *, volid: str, normalized: str, kind: str, start: bool
) -> Optional[dict[str, Any]]:
    if existing.strategy_name != RESTORE_BACKUP_STRATEGY:
        return None
    prev = dict((service.config or {}).get("vm_restore") or {})
    if prev.get("volid") not in (volid, normalized) and prev.get("normalized_volid") != normalized:
        return None
    return {
        "status": "queued",
        "job_id": existing.id,
        "volid": normalized,
        "kind": kind,
        "start": bool(prev.get("start", start)),
        "async": True,
    }


def _build_vm_restore_config(
    settings: BackupSettings,
    *,
    normalized: str,
    storage: Optional[str],
    kind: str,
    start: bool,
    vm_template_id: Optional[int],
    job_id: Optional[int] = None,
) -> dict[str, Any]:
    restore = {
        "volid": normalized,
        "storage": storage or (
            settings.client_storage if kind == "client" else settings.platform_storage
        ),
        "kind": kind,
        "start": bool(start),
        "vm_template_id": int(vm_template_id) if vm_template_id is not None else None,
        "status": "queued",
        "metadata_stale": False,
    }
    if job_id is not None:
        restore["job_id"] = job_id
    return restore


def enqueue_restore_backup_job(
    db: Session,
    service: Service,
    *,
    volid: str,
    storage: Optional[str] = None,
    start: bool = True,
    vm_template_id: Optional[int] = None,
) -> dict[str, Any]:
    """Enqueue a durable restore job; returns immediately with job metadata."""
    from app.dao.service_dao import ServiceDAO
    from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
    from app.models.service_vm import VMGuestState
    from app.services.deployment.registry import get_deployment_strategy_registry

    if service.service_type != ServiceType.VM:
        raise BackupConfigError("Not a VM service")

    settings = resolve_backup_settings(db, service)
    kind, normalized = _resolve_backup_kind(settings, storage or "", volid)

    existing = VMDeploymentJobDAO.get_active_for_service(db, service.id)
    if existing is not None:
        queued = _existing_restore_job_response(
            existing, service, volid=volid, normalized=normalized, kind=kind, start=start
        )
        if queued is not None:
            return queued
        raise BackupConfigError(
            f"Another deployment job is already active (job {existing.id}, "
            f"{existing.strategy_name}). Wait for it to finish before restoring."
        )

    strategy = get_deployment_strategy_registry().get(RESTORE_BACKUP_STRATEGY)
    service.config = {
        **dict(service.config or {}),
        "vm_restore": _build_vm_restore_config(
            settings,
            normalized=normalized,
            storage=storage,
            kind=kind,
            start=start,
            vm_template_id=vm_template_id,
        ),
    }
    if service.vm:
        service.vm.guest_state = VMGuestState.PROVISIONING
        service.vm.guest_last_error = None
    ServiceDAO.update(db, service)

    job = VMDeploymentJobDAO.create_job(
        db,
        service_id=service.id,
        strategy_name=strategy.name,
        step_names=strategy.step_names(),
        max_attempts=strategy.max_attempts,
    )
    service.config = {
        **dict(service.config or {}),
        "vm_restore": _build_vm_restore_config(
            settings,
            normalized=normalized,
            storage=storage,
            kind=kind,
            start=start,
            vm_template_id=vm_template_id,
            job_id=job.id,
        ),
    }
    ServiceDAO.update(db, service)

    logger.info(
        "Enqueued restore_backup job %s for service %s (volid=%s start=%s)",
        job.id,
        service.id,
        normalized,
        start,
    )
    return {
        "status": "queued",
        "job_id": job.id,
        "volid": normalized,
        "kind": kind,
        "vmid": None,
        "start": bool(start),
        "async": True,
        "waited": False,
    }


def _backup_notes_from_rows(rows: list, target: str) -> Optional[str]:
    for row in rows:
        row_vol = str(row.get("volid") or "")
        if row_vol == target or row_vol.endswith(":" + target) or target.endswith(row_vol):
            notes = row.get("notes") or row.get("note")
            if notes:
                return str(notes)
    return None


def _backup_lookup_storages(settings: BackupSettings, storage: Optional[str]) -> list[str]:
    storages: list[str] = []
    if storage:
        storages.append(str(storage))
    for candidate in (settings.client_storage, settings.platform_storage):
        if candidate and candidate not in storages:
            storages.append(candidate)
    return storages


async def lookup_backup_notes(
    db: Session,
    service: Service,
    *,
    volid: str,
    storage: Optional[str] = None,
) -> Optional[str]:
    """Return PBS/PVE notes for a backup volid (best-effort)."""
    settings = resolve_backup_settings(db, service)
    plugin, _cid, _node, vmid = await get_vm_backup_plugin(db, service)
    target = str(volid or "").strip()
    for st in _backup_lookup_storages(settings, storage):
        try:
            rows = await plugin.list_backups(st, vmid=vmid)
        except Exception:
            continue
        notes = _backup_notes_from_rows(rows, target)
        if notes:
            return notes
    return None


async def restore_service_backup(
    db: Session,
    service: Service,
    *,
    volid: str,
    storage: Optional[str] = None,
    wait: bool = False,
    start: bool = True,
    vm_template_id: Optional[int] = None,
) -> dict[str, Any]:
    """Restore a backup onto the service VMID.

    When ``wait`` is False (default), enqueue a durable deployment job and
    return immediately — preferred for client/billing HTTP paths that must not
    block on power-off + qmrestore. When ``wait`` is True, run synchronously
    (admin/tests) and apply metadata after completion.
    """
    if not wait:
        return enqueue_restore_backup_job(
            db,
            service,
            volid=volid,
            storage=storage,
            start=start,
            vm_template_id=vm_template_id,
        )

    from app.services.vm_identity_stamp import resolve_template_id_for_restore, stamp_vm_identity

    plugin, _cid, _node, _vmid = await get_vm_backup_plugin(db, service)
    await stop_guest_for_restore(plugin)
    result = await start_restore_task(
        db, service, volid=volid, storage=storage, start=start
    )
    await plugin.wait_for_proxmox_task(str(result["upid"]), timeout=7200.0)
    notes = await lookup_backup_notes(
        db, service, volid=str(result.get("volid") or volid), storage=storage
    )
    resolved = await resolve_template_id_for_restore(
        db,
        plugin,
        explicit_template_id=vm_template_id,
        volid=result.get("volid") or volid,
        notes=notes,
    )
    apply_metadata_after_restore(db, service, vm_template_id=resolved)
    try:
        await stamp_vm_identity(db, service, plugin)
    except Exception as exc:
        logger.warning("stamp_vm_identity after sync restore failed: %s", exc)
    result["waited"] = True
    result["start"] = bool(start)
    result["async"] = False
    return result


async def purge_client_backups(db: Session, service: Service) -> dict[str, Any]:
    """Delete all client-storage backups for the service VMID. Platform untouched."""
    try:
        settings = resolve_backup_settings(db, service)
    except BackupConfigError as exc:
        logger.info(
            "Skipping client backup purge for service %s: %s",
            service.id,
            exc,
        )
        return {"purged": 0, "skipped": True, "reason": str(exc)}

    try:
        plugin, _cid, _node, vmid = await get_vm_backup_plugin(db, service)
    except BackupConfigError as exc:
        logger.info(
            "Skipping client backup purge for service %s: %s",
            service.id,
            exc,
        )
        return {"purged": 0, "skipped": True, "reason": str(exc)}

    rows = await plugin.list_backups(settings.client_storage, vmid=vmid)
    purged = 0
    errors: List[str] = []
    for row in rows:
        volid = row.get("volid")
        if not volid:
            continue
        try:
            await plugin.delete_backup(settings.client_storage, str(volid))
            purged += 1
        except Exception as exc:
            logger.warning(
                "Failed to delete client backup %s for service %s: %s",
                volid,
                service.id,
                exc,
            )
            errors.append(f"{volid}: {exc}")
    return {
        "purged": purged,
        "skipped": False,
        "storage": settings.client_storage,
        "vmid": vmid,
        "errors": errors,
    }
