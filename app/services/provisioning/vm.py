"""VM service creation: catalog snapshot, IP, plan, optional auto-provision."""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.models.server_activity import ServerActivityEventType
from app.models.service import Service, ServiceStatus
from app.models.service_vm import VMGuestState
from app.services.provisioning.errors import ProvisioningError
from app.services.provisioning.request import ProvisionRequest, ProvisioningActor
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_success,
)
from app.services.vm_provisioning_service import VMProvisioningService
from app.services.vm_strategy_executor import schedule_vm_auto_provision
from app.services.vmid_allocator import reserve_vmid_for_service

logger = logging.getLogger(__name__)


def _set_guest_state(
    db: Session, service: Service, state: VMGuestState, error: Optional[str] = None
) -> None:
    if not service.vm:
        return
    service.vm.guest_state = state
    service.vm.guest_last_error = error
    ServiceDAO.update(db, service)


def _assign_vm_ip_or_fail(db: Session, service: Service, cluster_id: Optional[int]):
    allocation = VMIPAllocationDAO.assign_next_free_to_service(
        db,
        service_id=service.id,
        proxmox_cluster_id=cluster_id,
    )
    if allocation is not None:
        return allocation
    ServiceDAO.delete(db, service.id)
    raise ProvisioningError(
        "no_free_ip",
        (
            "No free VM IP address available to allocate. Add enabled addresses under VM IP allocations. "
            "For services without Proxmox cluster placement, only pool rows with no cluster restriction apply; "
            "when a cluster is set, rows restricted to that cluster (or unrestricted rows) may be used."
        ),
    )


def _validate_vm_provision_request(db: Session, req: ProvisionRequest) -> None:
    if (req.service_config or {}).get("server_group_id"):
        raise ProvisioningError(
            "invalid_request",
            "VM services cannot use server_group_id; use bare-metal provisioning for pooled servers",
        )
    if req.vm_template_id is not None and not req.product_code:
        raise ProvisioningError(
            "invalid_request",
            "vm_template_id requires product_code (template must be linked to that product in the catalog)",
        )
    if req.proxmox_vmid is not None and req.proxmox_cluster_id is None:
        raise ProvisioningError("invalid_request", "proxmox_vmid requires proxmox_cluster_id")
    if req.proxmox_cluster_id is not None:
        if ProxmoxInventoryDAO.get_cluster(db, req.proxmox_cluster_id) is None:
            raise ProvisioningError(
                "not_found", f"Unknown proxmox_cluster_id {req.proxmox_cluster_id}"
            )


def _reserve_requested_vmid(db: Session, service: Service, req: ProvisionRequest) -> None:
    if not (service.vm and req.proxmox_vmid is not None and req.proxmox_cluster_id is not None):
        return
    try:
        reserved_vmid = reserve_vmid_for_service(
            db,
            cluster_id=req.proxmox_cluster_id,
            service_id=service.id,
            requested_vmid=req.proxmox_vmid,
        )
    except ValueError as exc:
        ServiceDAO.delete(db, service.id)
        raise ProvisioningError("conflict", str(exc)) from exc
    service.vm.proxmox_vmid = int(reserved_vmid)
    db.commit()
    db.refresh(service)


def _attach_vm_plan(
    db: Session,
    service: Service,
    req: ProvisionRequest,
    effective_os_code: Optional[str],
    allocation,
) -> bool:
    cfg = {
        **(service.config or {}),
        "vm_ip_allocation_id": allocation.id,
        "vm_ip_address": allocation.ip_address,
    }
    has_plan = False
    if req.product_code and (req.vm_template_id or effective_os_code):
        cfg["vm_plan"] = VMProvisioningService.plan_provisioning(
            db=db,
            service_id=service.id,
            product_code=req.product_code,
            os_code=None,
            vm_template_id=req.vm_template_id,
            context={"service_id": service.id, "vm_ip_allocation_id": allocation.id},
        )
        has_plan = True
    service.config = cfg
    ServiceDAO.update(db, service)
    return has_plan


def _queue_auto_provision_if_requested(
    db: Session, service: Service, req: ProvisionRequest, has_plan: bool
) -> None:
    if not (req.auto_provision and has_plan):
        return
    try:
        schedule_vm_auto_provision(db, service)
    except ValueError as exc:
        _set_guest_state(db, service, VMGuestState.ERROR, error=str(exc))
        raise ProvisioningError("conflict", str(exc)) from exc
    logger.info("Auto-provisioning queued for VM service %s", service.id)


def create_vm_service(
    db: Session,
    req: ProvisionRequest,
    actor: ProvisioningActor,
    product_snapshot: dict[str, Any],
    effective_os_code: Optional[str],
) -> Service:
    _validate_vm_provision_request(db, req)

    node = (req.proxmox_node_name or "").strip() or None
    service = ServiceDAO.create_vm(
        db,
        name=req.name,
        owner_user_id=req.owner_user_id,
        external_service_id=req.external_service_id,
        status=ServiceStatus.PENDING,
        description=req.description,
        config=req.service_config or {},
        product_code=req.product_code,
        os_code=effective_os_code,
        product_snapshot=product_snapshot,
        provisioning_source=req.provisioning_source,
        proxmox_cluster_id=req.proxmox_cluster_id,
        proxmox_node_name=node,
        proxmox_vmid=req.proxmox_vmid,
        vm_template_id=req.vm_template_id,
    )
    db.refresh(service)
    if req.permission_set_id is not None:
        service.permission_set_id = req.permission_set_id
        ServiceDAO.update(db, service)

    _reserve_requested_vmid(db, service, req)
    allocation = _assign_vm_ip_or_fail(db, service, req.proxmox_cluster_id)
    log_details = {
        **actor.details,
        "vm_ip_allocation_id": allocation.id,
        "vm_ip_address": allocation.ip_address,
    }
    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create",
        source=actor.source,
        message=f"Creating VM service '{service.name}'",
        details=log_details,
    )

    has_plan = _attach_vm_plan(db, service, req, effective_os_code, allocation)
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create",
        source=actor.source,
        message=f"Created VM service '{service.name}'",
        details=log_details,
    )
    _queue_auto_provision_if_requested(db, service, req, has_plan)
    return service
