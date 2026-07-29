"""
VM provisioning facade: resolve placement, then enqueue a durable deployment job.

Execution itself lives in ``app/services/deployment`` (class-based strategies +
steps) and is carried out by the deployment worker. This module only prepares a
VM service for provisioning (placement + VMID) and creates the job row; it no
longer runs the clone/configure/power-on inline.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.dao.product_catalog_dao import VMTemplateDAO
from app.dao.service_dao import ServiceDAO
from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.service_vm import VMGuestState
from app.models.vm_deployment_job import VMDeploymentJob
from app.services.deployment.registry import get_deployment_strategy_registry
from app.services.proxmox_placement import auto_place_vm
from app.services.vm_install_type_strategy import resolve_vm_template_strategy
from app.services.vmid_allocator import reserve_vmid_for_service

logger = logging.getLogger(__name__)

APPLY_GUEST_PASSWORD_STRATEGY = "apply_guest_password"


def resolve_vm_strategy_name_for_service(db: Session, service: Service) -> str:
    """Resolve the deployment strategy name from ``vm_plan`` or the template os_type."""
    cfg = service.config or {}
    plan = cfg.get("vm_plan") or {}
    name = plan.get("strategy_name")
    if name:
        return str(name)
    if service.vm and service.vm.vm_template_id:
        tmpl = VMTemplateDAO.get_by_id(db, service.vm.vm_template_id)
        if tmpl:
            spec = resolve_vm_template_strategy(tmpl.os_type)
            return str(spec["strategy_name"])
    return "stub"


def _merge_provision_config(service: Service, patch: Dict[str, Any]) -> None:
    base = dict(service.config or {})
    prev = dict(base.get("vm_provision") or {})
    prev.update(patch)
    base["vm_provision"] = prev
    service.config = base


def prepare_vm_placement_for_provisioning(db: Session, service: Service) -> None:
    """
    Ensure a VM service has full Proxmox placement before provisioning:

    - Resolve the node (auto-place from inventory when none was supplied).
    - Reserve a VMID for the service (idempotent; returns the existing reservation
      when one already exists).

    Raises ``ValueError`` when placement cannot be resolved.
    """
    vm = service.vm
    if not vm or not vm.vm_template_id:
        raise ValueError("VM service has no vm_template_id")
    tmpl = VMTemplateDAO.get_by_id(db, vm.vm_template_id)
    if not tmpl:
        raise ValueError("VM template catalog row not found")

    cluster_id = vm.proxmox_cluster_id
    node_name = (vm.proxmox_node_name or "").strip()
    if not node_name:
        cluster_id, node_name = auto_place_vm(
            db,
            template_name=tmpl.proxmox_template_name,
            cluster_id=cluster_id,
            shared_storage=bool(tmpl.shared_storage),
        )
        vm.proxmox_cluster_id = cluster_id
        vm.proxmox_node_name = node_name

    if cluster_id is None:
        raise ValueError("Auto-provisioning requires a Proxmox cluster; none could be resolved")

    reserved_vmid = reserve_vmid_for_service(
        db,
        cluster_id=cluster_id,
        service_id=service.id,
        requested_vmid=vm.proxmox_vmid,
    )
    vm.proxmox_vmid = int(reserved_vmid)
    ServiceDAO.update(db, service)


def mark_vm_provision_queued(db: Session, service: Service) -> None:
    """Mirror a queued deployment on the service (visible to UI / guest sync)."""
    _merge_provision_config(
        service,
        {"status": "queued", "queued_at": datetime.now(timezone.utc).isoformat()},
    )
    if service.vm:
        service.vm.guest_state = VMGuestState.PROVISIONING
        service.vm.guest_last_error = None
    ServiceDAO.update(db, service)


def enqueue_vm_deployment_job(db: Session, service: Service) -> VMDeploymentJob:
    """
    Create a durable deployment job for a VM service (idempotent per active job).

    The service must already have a resolved strategy (``vm_plan`` or template
    os_type). Returns the existing non-terminal job when one is already queued /
    running / waiting so repeated create/provision calls don't stack jobs.

    Raises ``ValueError`` for non-VM services, terminated services, or a missing
    provisioning strategy.
    """
    if service.service_type != ServiceType.VM:
        raise ValueError("Not a VM service")
    if service.status == ServiceStatus.TERMINATED:
        raise ValueError("Cannot provision a terminated service")
    if not service.vm or not service.vm.vm_template_id:
        raise ValueError("VM service has no vm_template_id")

    strategy_name = resolve_vm_strategy_name_for_service(db, service)
    if strategy_name == "stub":
        raise ValueError(
            "Service has no provisioning strategy (stub). Ensure vm_plan exists or "
            "template os_type maps to a strategy."
        )
    strategy = get_deployment_strategy_registry().resolve(strategy_name)
    if strategy is None:
        raise ValueError(f"No deployment strategy registered for '{strategy_name}'")

    existing = VMDeploymentJobDAO.get_active_for_service(db, service.id)
    if existing is not None:
        return existing

    job = VMDeploymentJobDAO.create_job(
        db,
        service_id=service.id,
        strategy_name=strategy_name,
        step_names=strategy.step_names(),
        max_attempts=strategy.max_attempts,
    )
    mark_vm_provision_queued(db, service)
    logger.info(
        "Enqueued deployment job %s for VM service %s (strategy=%s)",
        job.id,
        service.id,
        strategy_name,
    )
    return job


def provision_vm_service_async(
    db: Session, service_id: int
) -> Tuple[Service, VMDeploymentJob]:
    """
    Resolve placement and enqueue a deployment job for a VM service.

    Non-blocking: the deployment worker performs the clone/configure/power-on.
    Returns the (refreshed) service and the enqueued job.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise ValueError("Service not found")
    if service.service_type != ServiceType.VM:
        raise ValueError("Not a VM service")
    if service.status == ServiceStatus.TERMINATED:
        raise ValueError("Cannot provision a terminated service")

    prepare_vm_placement_for_provisioning(db, service)
    job = enqueue_vm_deployment_job(db, service)
    db.refresh(service)
    return service, job


def schedule_vm_auto_provision(
    db: Session, service: Service, background_tasks: Optional[Any] = None
) -> VMDeploymentJob:
    """
    Shared create-path helper for admin and billing VM create endpoints: resolve
    placement, reserve the VMID, and enqueue the deployment job. Raises
    ``ValueError`` if placement or strategy resolution fails.

    ``background_tasks`` is accepted for backward compatibility but unused; the
    deployment worker (not FastAPI BackgroundTasks) executes the job now.
    """
    prepare_vm_placement_for_provisioning(db, service)
    return enqueue_vm_deployment_job(db, service)


def enqueue_apply_guest_password_job(
    db: Session, service: Service
) -> Optional[VMDeploymentJob]:
    """
    Enqueue a deferred guest-password apply job, or return an existing one.

    Returns ``None`` when another non-terminal job (typically provision) is
    already active — the desired password is expected to live in
    ``template_parameters`` and be applied by that job's configure step.
    """
    if service.service_type != ServiceType.VM:
        raise ValueError("Not a VM service")

    existing = VMDeploymentJobDAO.get_active_for_service(db, service.id)
    if existing is not None:
        if existing.strategy_name == APPLY_GUEST_PASSWORD_STRATEGY:
            return existing
        logger.info(
            "Skipping apply_guest_password enqueue for service %s; active job %s (%s)",
            service.id,
            existing.id,
            existing.strategy_name,
        )
        return None

    strategy = get_deployment_strategy_registry().get(APPLY_GUEST_PASSWORD_STRATEGY)
    job = VMDeploymentJobDAO.create_job(
        db,
        service_id=service.id,
        strategy_name=strategy.name,
        step_names=strategy.step_names(),
        max_attempts=strategy.max_attempts,
    )
    logger.info(
        "Enqueued apply_guest_password job %s for VM service %s",
        job.id,
        service.id,
    )
    return job
