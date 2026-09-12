"""Admin reassignment of a VM service's pool IP (release old, claim new, reset guest net)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.dao.service_dao import ServiceDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.models.service import Service, ServiceType
from app.models.server_activity import ServerActivityEventType
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_success,
)
from app.services.service_resource import vm_placement
from app.services.strategy_actions import StrategyActionError, run_action

logger = logging.getLogger(__name__)


class VmIpReassignError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _serialize_alloc(row) -> Dict[str, Any]:
    return {
        "id": row.id,
        "ip_address": row.ip_address,
        "subnet_mask": row.subnet_mask,
        "gateway": row.gateway,
        "bridge_name": row.bridge_name,
        "batch_tag": row.batch_tag,
        "cluster_ids": [c.id for c in (row.clusters or [])],
        "clusters": [{"id": c.id, "name": c.name} for c in (row.clusters or [])],
    }


def list_available_ips_for_service(db: Session, service: Service) -> Dict[str, Any]:
    """Return free pool IPs usable on this VM service's Proxmox cluster."""
    if service.service_type != ServiceType.VM or not service.vm:
        raise VmIpReassignError("Available IPs are only listed for VM services", 400)
    cluster_id, _, _ = vm_placement(service)
    current = None
    if service.vm.vm_ip_allocation_id:
        cur = VMIPAllocationDAO.get_by_id(db, service.vm.vm_ip_allocation_id)
        if cur:
            current = _serialize_alloc(cur)
    rows = VMIPAllocationDAO.list_all(
        db,
        enabled=True,
        assigned=False,
        cluster_id=cluster_id,
    )
    return {
        "proxmox_cluster_id": cluster_id,
        "current": current,
        "available": [_serialize_alloc(r) for r in rows],
    }


def _restore_previous_ip_assignment(
    db: Session, service: Service, old_id: int, cluster_id: int
) -> None:
    try:
        VMIPAllocationDAO.assign_specific_to_service(
            db,
            service_id=service.id,
            allocation_id=old_id,
            proxmox_cluster_id=cluster_id,
        )
    except ValueError:
        logger.exception(
            "Failed to restore previous IP allocation %s for service %s",
            old_id,
            service.id,
        )


async def _maybe_reset_network_after_reassign(
    db: Session, service: Service, reset_network: bool
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not reset_network:
        return None, None
    try:
        result = await run_action(db, service, "reset_network", {}, "admin")
        db.commit()
        return result, None
    except StrategyActionError as exc:
        logger.warning(
            "IP reassigned for service %s but reset_network failed: %s",
            service.id,
            exc,
        )
        return None, str(exc)
    except Exception as exc:
        logger.exception("IP reassigned for service %s but reset_network raised", service.id)
        return None, str(exc) or "Guest network reset failed"


async def reassign_vm_ip(
    db: Session,
    service: Service,
    *,
    allocation_id: int,
    reset_network: bool = True,
    source: str = "admin_api",
) -> Dict[str, Any]:
    """
    Release the service's current VM IP pool row, claim ``allocation_id``, update
    service config, and optionally run the strategy ``reset_network`` action.

    For ``cloudinit_clone``, reset_network regenerates cloud-init and **reboots**
    the guest so the new address is applied.
    """
    if service.service_type != ServiceType.VM or not service.vm:
        raise VmIpReassignError("IP reassignment is only supported for VM services", 400)

    cluster_id, _, _ = vm_placement(service)
    old_alloc = None
    if service.vm.vm_ip_allocation_id:
        old_alloc = VMIPAllocationDAO.get_by_id(db, service.vm.vm_ip_allocation_id)

    if old_alloc and old_alloc.id == allocation_id:
        raise VmIpReassignError("Selected IP is already assigned to this service", 409)

    new_preview = VMIPAllocationDAO.get_by_id(db, allocation_id)
    if not new_preview:
        raise VmIpReassignError(f"VM IP allocation {allocation_id} not found", 404)

    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="reassign_vm_ip",
        source=source,
        message=(
            f"Reassigning VM IP from "
            f"{(old_alloc.ip_address if old_alloc else 'none')} to {new_preview.ip_address}"
        ),
        details={
            "old_allocation_id": old_alloc.id if old_alloc else None,
            "old_ip_address": old_alloc.ip_address if old_alloc else None,
            "new_allocation_id": allocation_id,
            "new_ip_address": new_preview.ip_address,
            "reset_network": reset_network,
            "proxmox_cluster_id": cluster_id,
        },
    )

    old_ip = old_alloc.ip_address if old_alloc else None
    old_id = old_alloc.id if old_alloc else None

    VMIPAllocationDAO.release_for_service(db, service.id)
    try:
        new_alloc = VMIPAllocationDAO.assign_specific_to_service(
            db,
            service_id=service.id,
            allocation_id=allocation_id,
            proxmox_cluster_id=cluster_id,
        )
    except ValueError as exc:
        if old_id is not None:
            _restore_previous_ip_assignment(db, service, old_id, cluster_id)
        raise VmIpReassignError(str(exc), 409) from exc

    cfg = dict(service.config or {})
    cfg["vm_ip_allocation_id"] = new_alloc.id
    cfg["vm_ip_address"] = new_alloc.ip_address
    service.config = cfg
    ServiceDAO.update(db, service)
    db.commit()
    db.refresh(service)

    network_result, network_error = await _maybe_reset_network_after_reassign(
        db, service, reset_network
    )

    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="reassign_vm_ip",
        source=source,
        message=f"Reassigned VM IP to {new_alloc.ip_address}",
        details={
            "old_allocation_id": old_id,
            "old_ip_address": old_ip,
            "new_allocation_id": new_alloc.id,
            "new_ip_address": new_alloc.ip_address,
            "reset_network": reset_network,
            "network_error": network_error,
            "network_result": network_result,
        },
    )

    out: Dict[str, Any] = {
        "status": "ok" if not network_error else "partial",
        "old_ip_address": old_ip,
        "old_allocation_id": old_id,
        "vm_ip_allocation_id": new_alloc.id,
        "vm_ip_address": new_alloc.ip_address,
        "allocation": _serialize_alloc(new_alloc),
        "reset_network": reset_network,
        "network": network_result,
    }
    if network_error:
        out["network_error"] = network_error
    return out
