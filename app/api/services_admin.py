"""
Admin API endpoints for managing services and external users.

These endpoints are for admin users to view and manage services
and external users created via billing integrations.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
import asyncio
from sqlalchemy import or_
from sqlalchemy.orm import Session
from typing import Annotated, List, Optional, Any, Dict
from pydantic import BaseModel, Field
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.core.auth import require_admin
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.dao.ipam_dao import IPAMDAO
from app.services.proxy_provisioning import assignment_payload, auto_assign_proxy_ips, resolve_proxy_ip_request
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.permission_set_dao import PermissionSetDAO
from app.services.client_permission_resolver import resolve_client_permissions
from app.models.service import Service, ServiceStatus, ServiceType, ProvisioningSource
from app.models.service_bare_metal import ServiceBareMetal
from app.models.user import User
from app.services.service_product_snapshot import build_product_snapshot
from app.services.vm_provisioning_service import VMProvisioningService
from app.services.vmid_allocator import reserve_vmid_aligned_with_proxmox, reserve_vmid_for_service
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_success,
)

_MSG_UNIQUE_SERVICE_NAME = "Unique service name"
_MSG_NOT_A_VM_SERVICE = "Not a VM service"
_MSG_VM_SERVICE_NOT_FOUND = "VM service not found"
_MSG_OWNER_USER_NOT_FOUND = "Owner user not found"
from app.services.service_resource import service_linked_server, service_server_id_for_response, vm_placement
from app.models.server_activity import ServerActivityEventType
from app.services.vm_strategy_executor import (
    provision_vm_service_async,
    resolve_vm_strategy_name_for_service,
    schedule_vm_auto_provision,
)
from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
from app.services.proxmox_placement import (
    ProxmoxPlacementError,
    resolve_proxmox_plugin_for_service,
)
from app.plugins.base import PowerState
from app.plugins.proxmox import ConsoleTypeUnavailable
from app.models.service_vm import VMGuestState
from app.services.vm_guest_credentials import session_guest_fields
from app.services.vm_vnc_ticket_service import (
    build_relative_error_url,
    build_relative_launch_url,
    mint_launch_ticket,
    mint_ws_session,
)
from app.api.ipmi_kvm import kvm_popup_redirect
from app.services.ipmi_kvm_ticket_service import build_relative_error_url as kvm_error_url
from app.api.sol import perform_sol_send, sol_popup_redirect
from app.services.sol.ticket_service import build_relative_error_url as sol_error_url
from app.schemas.sol import SolSendRequest, SolSendResponse
from app.schemas.virtual_media import VirtualMediaInsertRequest, VirtualMediaStatusResponse
from app.api.virtual_media import perform_eject, perform_insert, perform_status
from app.schemas.vm_vnc import VmConsoleTypesResponse, VmVncSessionResponse
from app.api.vm_backup_routes import BackupCreateBody, BackupMutateBody, map_backup_error
from app.services.vm_backup_service import (
    create_client_backup,
    delete_client_backup,
    list_service_backups_and_jobs,
    purge_client_backups,
    restore_service_backup,
)
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(prefix="/admin/services", tags=["admin-services"])


class ExternalUserResponse(BaseModel):
    id: int
    integration_id: int
    integration_name: str
    external_user_id: str
    external_username: Optional[str] = None
    external_email: Optional[str] = None
    created_at: str
    updated_at: str
    service_count: int = 0

    class Config:
        from_attributes = True


class ServiceResponse(BaseModel):
    id: int
    name: str
    external_service_id: Optional[str] = None
    owner_user_id: Optional[int] = None
    owner_username: Optional[str] = None
    owner_email: Optional[str] = None
    server_id: Optional[int] = None
    server_name: str = ""
    external_user_id: Optional[int] = None
    external_user_external_id: Optional[str] = None
    external_username: Optional[str] = None
    external_email: Optional[str] = None
    service_type: Optional[str] = None
    provisioning_source: Optional[str] = None
    proxmox_cluster_id: Optional[int] = None
    proxmox_node_name: Optional[str] = None
    proxmox_vmid: Optional[int] = None
    product_code: Optional[str] = None
    os_code: Optional[str] = None
    vm_template_id: Optional[int] = None
    vm_ip_allocation_id: Optional[int] = Field(
        default=None,
        description="VM IP pool row linked on service_vm (primary relational link)",
    )
    vm_ip_address: Optional[str] = Field(
        default=None,
        description="Public / customer IP from the VM IP pool when allocated",
    )
    vm_strategy_name: Optional[str] = Field(
        default=None,
        description="Resolved OS strategy (e.g. cloudinit_clone) from vm_plan or template os_type",
    )
    vm_guest_state: Optional[str] = None
    vm_guest_last_error: Optional[str] = None
    accepts_ssh_key: bool = False
    has_ssh_public_keys: bool = False
    ssh_public_keys_text: str = ""
    needs_ssh_key_prompt: bool = False
    status: str
    description: Optional[str] = None
    config: Optional[dict] = None
    permission_set_id: Optional[int] = None
    permission_set_name: Optional[str] = None
    permission_overrides: Optional[Dict[str, bool]] = None
    proxy_assignments: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="For http_proxy services: assigned IP(s) + credentials + ready-to-use proxy URLs",
    )
    created_at: str
    updated_at: str
    terminated_at: Optional[str] = None

    class Config:
        from_attributes = True


class VmReinstallBody(BaseModel):
    vm_template_id: Optional[int] = Field(
        None, description="Optional new catalog VM template (must be linked to product)"
    )
    ssh_public_keys: Optional[Any] = Field(
        None,
        description="Multiline text or list of OpenSSH public keys to store before reinstall",
    )


class VmSshKeysBody(BaseModel):
    ssh_public_keys: Any = Field(
        ...,
        description="Multiline text or list of OpenSSH public keys (one key per line)",
    )


class ServicePermissionsAssignBody(BaseModel):
    permission_set_id: Optional[int] = Field(None, description="null clears the service-level preset")
    permission_overrides: Optional[Dict[str, bool]] = Field(
        None, description="Sparse per-key overrides; null clears all overrides"
    )


class InternalTestVMServiceCreate(BaseModel):
    """Create a VM service without billing / external user (lab or QA)."""

    name: str = Field(..., description=_MSG_UNIQUE_SERVICE_NAME)
    product_code: str
    vm_template_id: int = Field(..., description="Catalog VM template id (must be linked to product; sets OS strategy)")
    proxmox_cluster_id: int
    proxmox_node_name: str
    proxmox_vmid: int
    description: Optional[str] = None
    service_config: Optional[Dict[str, Any]] = None


class AdminVmServiceCreate(BaseModel):
    """
    Create a VM service in ``pending`` status (no RackFlow Server row).

    Proxmox placement is optional — omit cluster/node/vmid until the guest exists or is placed.
    """

    name: str = Field(..., description=_MSG_UNIQUE_SERVICE_NAME)
    product_code: str
    vm_template_id: int = Field(..., description="Catalog VM template id linked to product")
    description: Optional[str] = None
    service_config: Optional[Dict[str, Any]] = None
    owner_user_id: Optional[int] = Field(
        None,
        description=(
            "users.id canonical owner in RackFlow. provisioning_source is "
            "billing when this user has a linked billing identity, internal otherwise."
        ),
    )
    external_service_id: Optional[str] = Field(None, description="Optional external line-item id (e.g. WHMCS service id)")
    proxmox_cluster_id: Optional[int] = None
    proxmox_node_name: Optional[str] = None
    proxmox_vmid: Optional[int] = None
    auto_provision: bool = Field(
        default=True,
        description=(
            "When true, resolve placement (auto-place from inventory if node omitted), reserve a VMID, "
            "and provision the guest in the background. Set false to create a pending service for the "
            "manual two-phase flow (POST /admin/services/{id}/provision-vm)."
        ),
    )


class AdminHttpProxyServiceCreate(BaseModel):
    """
    Create an http_proxy service (``services`` + ``service_bare_metal`` with
    no linked Server) with IP(s) auto-assigned from IPAM.

    ``ip_count``/``subnet_id``/``subnet_group_id``/``allocation_strategy``
    override the product/family catalog defaults when set; a bare service
    with no ``product_code`` falls back to a single auto-picked IP.
    """

    name: str = Field(..., description=_MSG_UNIQUE_SERVICE_NAME)
    product_code: Optional[str] = Field(None, description="Catalog product code (http_proxy family)")
    description: Optional[str] = None
    service_config: Optional[Dict[str, Any]] = None
    owner_user_id: Optional[int] = Field(
        None,
        description=(
            "users.id canonical owner in RackFlow. provisioning_source is "
            "billing when this user has a linked billing identity, internal otherwise."
        ),
    )
    external_service_id: Optional[str] = Field(None, description="Optional external line-item id (e.g. WHMCS service id)")
    ip_count: Optional[int] = Field(None, description="Override how many IPs to auto-assign (default 1)")
    subnet_id: Optional[int] = Field(None, description="Override which subnet to assign from (wins over subnet_group_id)")
    subnet_group_id: Optional[int] = Field(None, description="Override which proxy subnet group to assign from")
    allocation_strategy: Optional[str] = Field(None, description="Override allocation strategy")


class ServiceOwnerAssignBody(BaseModel):
    owner_user_id: Optional[int] = Field(
        None,
        description="users.id canonical owner; null unassigns owner",
    )


class VmPowerActionBody(BaseModel):
    action: str = Field(..., description="on | off | reboot | reset")


class VmPlacementUpdateBody(BaseModel):
    proxmox_cluster_id: int
    proxmox_node_name: str
    proxmox_vmid: Optional[int] = None


class ServiceStatusUpdateBody(BaseModel):
    status: str = Field(..., description="active | suspended | terminated | pending")


class DeploymentJobStepResponse(BaseModel):
    id: int
    position: int
    name: str
    status: str
    attempt_count: int
    message: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    class Config:
        from_attributes = True


class DeploymentJobResponse(BaseModel):
    id: int
    service_id: int
    strategy_name: str
    status: str
    current_step_index: int
    attempt: int
    max_attempts: int
    error_message: Optional[str] = None
    next_run_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: str
    updated_at: str
    steps: List[DeploymentJobStepResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


def _deployment_job_to_response(job) -> DeploymentJobResponse:
    def _iso(dt):
        return dt.isoformat() if dt else None

    steps = [
        DeploymentJobStepResponse(
            id=s.id,
            position=s.position,
            name=s.name,
            status=s.status.value if hasattr(s.status, "value") else str(s.status),
            attempt_count=s.attempt_count,
            message=s.message,
            detail=s.detail,
            started_at=_iso(s.started_at),
            finished_at=_iso(s.finished_at),
        )
        for s in sorted(job.steps, key=lambda x: x.position)
    ]
    return DeploymentJobResponse(
        id=job.id,
        service_id=job.service_id,
        strategy_name=job.strategy_name,
        status=job.status.value if hasattr(job.status, "value") else str(job.status),
        current_step_index=job.current_step_index,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        error_message=job.error_message,
        next_run_at=_iso(job.next_run_at),
        started_at=_iso(job.started_at),
        finished_at=_iso(job.finished_at),
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        steps=steps,
    )


def _service_to_admin_response(db: Session, service) -> ServiceResponse:
    server = service_linked_server(db, service)
    owner = service.owner_user
    billed = owner if (owner is not None and owner.billing_integration_id) else None
    src = service.provisioning_source or ProvisioningSource.BILLING
    cid, node, vmid = vm_placement(service)
    vm_ip_allocation_id = None
    vm_ip_address = None
    if service.vm:
        vm_ip_allocation_id = service.vm.vm_ip_allocation_id
        if service.vm.vm_ip_allocation:
            vm_ip_address = service.vm.vm_ip_allocation.ip_address
        elif service.config:
            vm_ip_address = (service.config or {}).get("vm_ip_address")
            vm_ip_allocation_id = vm_ip_allocation_id or (service.config or {}).get("vm_ip_allocation_id")
    from app.services.ssh_public_keys import ssh_key_fields_for_service

    vm_strategy_name = None
    ssh_fields = {
        "accepts_ssh_key": False,
        "has_ssh_public_keys": False,
        "ssh_public_keys_text": "",
        "needs_ssh_key_prompt": False,
    }
    if service.service_type == ServiceType.VM:
        vm_strategy_name = resolve_vm_strategy_name_for_service(db, service)
        ssh_fields = ssh_key_fields_for_service(db, service)
    proxy_assignments = None
    if service.service_type == ServiceType.HTTP_PROXY:
        proxy_assignments = [
            assignment_payload(a) for a in IPAMDAO.get_assignment_by_service(db, service.id)
        ]
    return ServiceResponse(
        id=service.id,
        name=service.name,
        external_service_id=service.external_service_id,
        owner_user_id=service.owner_user_id,
        owner_username=owner.username if owner else None,
        owner_email=owner.email if owner else None,
        server_id=service_server_id_for_response(service),
        server_name=server.name if server else "",
        external_user_id=billed.id if billed else None,
        external_user_external_id=billed.external_user_id if billed else None,
        external_username=billed.external_username if billed else None,
        external_email=billed.external_email if billed else None,
        service_type=service.service_type.value if service.service_type else None,
        provisioning_source=src.value if hasattr(src, "value") else str(src),
        proxmox_cluster_id=cid,
        proxmox_node_name=node,
        proxmox_vmid=vmid,
        product_code=service.product_code,
        os_code=service.os_code,
        vm_template_id=service.vm.vm_template_id if service.vm else None,
        vm_ip_allocation_id=vm_ip_allocation_id,
        vm_ip_address=vm_ip_address,
        vm_strategy_name=vm_strategy_name,
        vm_guest_state=service.vm.guest_state.value if service.vm and service.vm.guest_state else None,
        vm_guest_last_error=service.vm.guest_last_error if service.vm else None,
        accepts_ssh_key=bool(ssh_fields.get("accepts_ssh_key")),
        has_ssh_public_keys=bool(ssh_fields.get("has_ssh_public_keys")),
        ssh_public_keys_text=str(ssh_fields.get("ssh_public_keys_text") or ""),
        needs_ssh_key_prompt=bool(ssh_fields.get("needs_ssh_key_prompt")),
        status=service.status.value,
        description=service.description,
        config=service.config,
        permission_set_id=service.permission_set_id,
        permission_set_name=service.permission_set.name if service.permission_set else None,
        permission_overrides=service.permission_overrides,
        proxy_assignments=proxy_assignments,
        created_at=service.created_at.isoformat(),
        updated_at=service.updated_at.isoformat(),
        terminated_at=service.terminated_at.isoformat() if service.terminated_at else None,
    )


async def _admin_get_vm_plugin(db: Session, service: Service):
    """Resolve a live Proxmox plugin for a VM service.

    ``proxmox_node_name`` is treated as a cache: if it's empty or stale
    (guest migrated), this searches the cluster for the VMID's current node
    and updates the cache before erroring. Returns ``(plugin, cluster_id,
    node_name, vmid)``.
    """
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NOT_A_VM_SERVICE)
    try:
        return await resolve_proxmox_plugin_for_service(db, service)
    except ProxmoxPlacementError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _set_guest_state(db: Session, service: Service, state: VMGuestState, error: Optional[str] = None) -> None:
    if not service.vm:
        return
    service.vm.guest_state = state
    service.vm.guest_last_error = error
    ServiceDAO.update(db, service)


async def _sync_guest_state_from_proxmox(db: Session, service: Service) -> None:
    """
    Refresh ``service_vm.guest_state`` from live Proxmox (exists + power).

    Used on VM detail page load so the UI does not show a stale cached state.
    Skips while provisioning is in flight, or when placement is incomplete.
    Proxmox unreachable leaves the previous state and records ``guest_last_error``.
    """
    if not service.vm:
        return
    prov_status = ((service.config or {}).get("vm_provision") or {}).get("status")
    if service.vm.guest_state == VMGuestState.PROVISIONING or prov_status in ("queued", "running"):
        return
    cid, _node, vmid = vm_placement(service)
    if cid is None or vmid is None:
        return
    try:
        plugin, _cid, _node, _vmid = await _admin_get_vm_plugin(db, service)
    except HTTPException:
        return
    try:
        exists = await plugin.vm_exists()
    except Exception as exc:
        logger.warning("Proxmox guest sync failed (exists) for service %s: %s", service.id, exc)
        _set_guest_state(
            db,
            service,
            service.vm.guest_state,
            error=f"Proxmox sync failed: {exc}",
        )
        return
    if not exists:
        _set_guest_state(db, service, VMGuestState.DESTROYED, error=None)
        return
    try:
        power = await plugin.get_power_state()
    except Exception as exc:
        logger.warning("Proxmox guest sync failed (power) for service %s: %s", service.id, exc)
        _set_guest_state(
            db,
            service,
            service.vm.guest_state,
            error=f"Proxmox sync failed: {exc}",
        )
        return
    if power == PowerState.ON:
        _set_guest_state(db, service, VMGuestState.RUNNING, error=None)
    elif power == PowerState.OFF:
        _set_guest_state(db, service, VMGuestState.STOPPED, error=None)
    # UNKNOWN: leave stored state unchanged


@router.get("/external-users", response_model=List[ExternalUserResponse], responses=COMMON_ERROR_RESPONSES)
async def list_external_users(
    integration_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    *,
    auth: AdminDep,
    db: DbDep,
):
    """List all users with a linked billing identity.

    Named "external users" for backwards compatibility with the admin UI,
    but these are now just ``User`` rows with ``billing_integration_id`` set.
    """
    query = db.query(User).filter(User.billing_integration_id.isnot(None))
    if integration_id:
        query = query.filter(User.billing_integration_id == integration_id)

    users = query.order_by(User.created_at.desc()).offset(skip).limit(limit).all()

    result = []
    for u in users:
        services = ServiceDAO.get_by_owner_user(db, u.id)
        result.append(
            ExternalUserResponse(
                id=u.id,
                integration_id=u.billing_integration_id,
                integration_name=u.billing_integration.name if u.billing_integration else "",
                external_user_id=u.external_user_id or "",
                external_username=u.external_username,
                external_email=u.external_email,
                created_at=u.created_at.isoformat(),
                updated_at=u.updated_at.isoformat(),
                service_count=len(services),
            )
        )

    return result


@router.get("/external-users/{external_user_id}", response_model=ExternalUserResponse, responses=COMMON_ERROR_RESPONSES)
async def get_external_user(
    external_user_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """Get billing-identity details for a user (see ``list_external_users``)."""
    user = UserDAO.get_by_id(db, external_user_id)
    if not user or not user.billing_integration_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="External user not found",
        )

    services = ServiceDAO.get_by_owner_user(db, user.id)

    return ExternalUserResponse(
        id=user.id,
        integration_id=user.billing_integration_id,
        integration_name=user.billing_integration.name if user.billing_integration else "",
        external_user_id=user.external_user_id or "",
        external_username=user.external_username,
        external_email=user.external_email,
        created_at=user.created_at.isoformat(),
        updated_at=user.updated_at.isoformat(),
        service_count=len(services),
    )


@router.get("", response_model=List[ServiceResponse], responses=COMMON_ERROR_RESPONSES)
async def list_services(
    q: Optional[str] = None,
    status_filter: Optional[str] = None,
    external_user_id: Optional[int] = None,
    owner_user_id: Optional[int] = None,
    server_id: Optional[int] = None,
    provisioning_source: Optional[str] = None,
    service_type: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    *,
    auth: AdminDep,
    db: DbDep,
):
    """List all services"""
    query = db.query(Service)

    if q and q.strip():
        needle = f"%{q.strip()}%"
        query = (
            query.outerjoin(User, User.id == Service.owner_user_id)
            .filter(
                or_(
                    Service.name.ilike(needle),
                    Service.external_service_id.ilike(needle),
                    User.username.ilike(needle),
                    User.external_username.ilike(needle),
                )
            )
        )

    if service_type:
        try:
            st = ServiceType(service_type.lower())
            query = query.filter(Service.service_type == st)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid service_type. Use 'bare_metal', 'vm', or 'http_proxy'.",
            )

    if status_filter:
        try:
            status_enum = ServiceStatus(status_filter.lower())
            query = query.filter(Service.status == status_enum)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status: {status_filter}",
            )

    if external_user_id is not None:
        # Kept for backwards compatibility: "external user id" is now simply
        # the owner user's id (a billing identity lives on the User row).
        query = query.filter(Service.owner_user_id == external_user_id)

    if owner_user_id is not None:
        query = query.filter(Service.owner_user_id == owner_user_id)

    if server_id is not None:
        query = query.join(ServiceBareMetal, ServiceBareMetal.service_id == Service.id).filter(
            ServiceBareMetal.server_id == server_id
        )

    if provisioning_source:
        try:
            ps = ProvisioningSource(provisioning_source.lower())
            query = query.filter(Service.provisioning_source == ps)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid provisioning_source. Use 'billing' or 'internal'.",
            )

    services = query.order_by(Service.name).offset(skip).limit(limit).all()

    return [_service_to_admin_response(db, s) for s in services]


@router.get("/unassigned", response_model=List[ServiceResponse], responses=COMMON_ERROR_RESPONSES)
async def list_unassigned_services(
    skip: int = 0,
    limit: int = 100,
    *,
    auth: AdminDep,
    db: DbDep,
):
    services = (
        db.query(Service)
        .filter(Service.owner_user_id.is_(None))
        .order_by(Service.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [_service_to_admin_response(db, s) for s in services]


@router.get("/vm", response_model=List[ServiceResponse], responses=COMMON_ERROR_RESPONSES)
async def list_vm_services_admin(
    status_filter: Optional[str] = None,
    owner_user_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    *,
    auth: AdminDep,
    db: DbDep,
):
    query = db.query(Service).filter(Service.service_type == ServiceType.VM)
    if status_filter:
        try:
            status_enum = ServiceStatus(status_filter.lower())
            query = query.filter(Service.status == status_enum)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid status: {status_filter}") from exc
    if owner_user_id is not None:
        query = query.filter(Service.owner_user_id == owner_user_id)
    services = query.order_by(Service.name).offset(skip).limit(limit).all()
    return [_service_to_admin_response(db, s) for s in services]


@router.get("/bare-metal", response_model=List[ServiceResponse], responses=COMMON_ERROR_RESPONSES)
async def list_bare_metal_services_admin(
    status_filter: Optional[str] = None,
    owner_user_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    *,
    auth: AdminDep,
    db: DbDep,
):
    query = db.query(Service).filter(Service.service_type != ServiceType.VM)
    if status_filter:
        try:
            status_enum = ServiceStatus(status_filter.lower())
            query = query.filter(Service.status == status_enum)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid status: {status_filter}") from exc
    if owner_user_id is not None:
        query = query.filter(Service.owner_user_id == owner_user_id)
    services = query.order_by(Service.name).offset(skip).limit(limit).all()
    return [_service_to_admin_response(db, s) for s in services]


@router.get("/vm/{service_id}", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def get_vm_service_admin(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """
    Return a VM service. Guest power/existence is refreshed from Proxmox on each
    load (unless provisioning is in flight) so the detail page shows live state.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_VM_SERVICE_NOT_FOUND)
    await _sync_guest_state_from_proxmox(db, service)
    db.refresh(service)
    return _service_to_admin_response(db, service)


@router.get("/{service_id}/deployment-jobs", response_model=List[DeploymentJobResponse], responses=COMMON_ERROR_RESPONSES)
async def list_deployment_jobs(
    service_id: int,
    limit: int = 50,
    *,
    auth: AdminDep,
    db: DbDep,
):
    """List deployment jobs (with ordered step timelines) for a VM service."""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    jobs = VMDeploymentJobDAO.list_by_service(db, service_id, limit=limit)
    return [_deployment_job_to_response(j) for j in jobs]


@router.get("/{service_id}/deployment-jobs/{job_id}", response_model=DeploymentJobResponse, responses=COMMON_ERROR_RESPONSES)
async def get_deployment_job(
    service_id: int,
    job_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """Return a single deployment job and its ordered step timeline."""
    job = VMDeploymentJobDAO.get_by_id(db, job_id)
    if not job or job.service_id != service_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment job not found")
    return _deployment_job_to_response(job)


@router.get("/bare-metal/{service_id}", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def get_bare_metal_service_admin(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.service_type == ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bare metal service not found")
    return _service_to_admin_response(db, service)


@router.post("/{service_id}/provision-vm", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
def admin_provision_vm_service(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """
    Resolve placement (auto-place + reserve VMID if needed) and enqueue a durable
    deployment job. Non-blocking: the deployment worker clones the catalog
    template, applies sizing, and (per strategy) configures cloud-init / waits for
    the guest agent, then powers on. Poll ``vm_guest_state`` /
    ``config.vm_provision`` or ``GET .../deployment-jobs`` for progress.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_VM_SERVICE_NOT_FOUND)
    try:
        service, _job = provision_vm_service_async(db, service_id)
    except ValueError as exc:
        _set_guest_state(db, service, VMGuestState.ERROR, error=str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Admin VM provision enqueue failed for service %s", service_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return _service_to_admin_response(db, service)


@router.put("/{service_id}/vm/placement", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def admin_update_vm_placement(
    service_id: int,
    body: VmPlacementUpdateBody,
    auth: AdminDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.service_type != ServiceType.VM or not service.vm:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_VM_SERVICE_NOT_FOUND)
    if ProxmoxInventoryDAO.get_cluster(db, body.proxmox_cluster_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proxmox cluster not found")
    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="update_vm_placement",
        source="admin_api",
        message="Updating VM placement",
        details={"cluster_id": body.proxmox_cluster_id, "node_name": body.proxmox_node_name, "requested_vmid": body.proxmox_vmid},
    )

    requested = body.proxmox_vmid
    node_name = (body.proxmox_node_name or "").strip()
    try:
        if node_name:
            reserved_vmid = await reserve_vmid_aligned_with_proxmox(
                db,
                cluster_id=body.proxmox_cluster_id,
                service_id=service.id,
                node_name=node_name,
                requested_vmid=requested,
            )
        else:
            reserved_vmid = reserve_vmid_for_service(
                db,
                cluster_id=body.proxmox_cluster_id,
                service_id=service.id,
                requested_vmid=requested,
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    service.vm.proxmox_cluster_id = body.proxmox_cluster_id
    service.vm.proxmox_node_name = node_name
    service.vm.proxmox_vmid = int(reserved_vmid)
    ServiceDAO.update(db, service)
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="update_vm_placement",
        source="admin_api",
        message="Updated VM placement",
        details={"cluster_id": body.proxmox_cluster_id, "node_name": body.proxmox_node_name, "vmid": int(reserved_vmid)},
    )
    return _service_to_admin_response(db, service)


@router.post("/{service_id}/vm/power", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def admin_vm_power_action(
    service_id: int,
    body: VmPowerActionBody,
    auth: AdminDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    plugin, _cid, _node, _vmid = await _admin_get_vm_plugin(db, service)
    action = (body.action or "").strip().lower()
    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.POWER,
        action=action,
        source="admin_api",
        message=f"VM power action '{action}' requested",
        details={"service_id": service.id},
    )
    if action == "on":
        ok = await plugin.power_on()
        if ok:
            _set_guest_state(db, service, VMGuestState.RUNNING)
    elif action == "off":
        ok = await plugin.power_off(force=False)
        if ok:
            _set_guest_state(db, service, VMGuestState.STOPPED)
    elif action in ("reboot", "reset"):
        ok = await plugin.power_reset()
        if ok:
            _set_guest_state(db, service, VMGuestState.RUNNING)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid action; use on|off|reboot|reset")
    if not ok:
        _set_guest_state(db, service, VMGuestState.ERROR, error=f"Power action '{action}' failed")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Power action '{action}' failed")
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.POWER,
        action=action,
        source="admin_api",
        message=f"VM power action '{action}' completed",
        details={"service_id": service.id},
    )
    db.refresh(service)
    return _service_to_admin_response(db, service)


@router.get("/{service_id}/vm/console-types", response_model=VmConsoleTypesResponse, responses=COMMON_ERROR_RESPONSES)
async def admin_get_vm_console_types(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """Report which console types (noVNC/serial) this VM actually supports.

    Fetched by the admin UI before showing the "Open Console" control(s), so
    it can offer a picker only when the VM genuinely supports both.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    plugin, _cid, _node, _vmid = await _admin_get_vm_plugin(db, service)
    try:
        available = await plugin.get_available_console_types()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not reach Proxmox: {exc}"
        ) from exc
    return VmConsoleTypesResponse(**available)


@router.post("/{service_id}/vm/vnc-session", response_model=VmVncSessionResponse, responses=COMMON_ERROR_RESPONSES)
async def admin_create_vm_vnc_session(
    service_id: int,
    console_type: Optional[str] = None,
    *,
    auth: AdminDep,
    db: DbDep,
):
    """Mint a VNC/serial console session for a VM service (admin).

    Opens a Proxmox console proxy for the guest and wraps it in a Rackflow
    WS session token; the browser never sees Proxmox account credentials.
    ``console_type`` (``"vnc"``/``"serial"``) picks a specific type when the
    VM supports both; omit it to use the default preference (noVNC first).
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    plugin, cid, node, vmid = await _admin_get_vm_plugin(db, service)
    try:
        power_state = await plugin.get_power_state()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not reach Proxmox: {exc}"
        ) from exc
    if power_state != PowerState.ON:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="VM must be running to open a console")
    try:
        console = await plugin.open_console_proxy(console_type=console_type)
    except ConsoleTypeUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to open console: {exc}"
        ) from exc
    session = mint_ws_session(
        service.id, cid, node, vmid, console["port"], console["ticket"], console["console_type"]
    )
    logger.info(
        "Admin API: minted VM %s session for service %s", console["console_type"], service.id
    )
    return VmVncSessionResponse(
        ws_token=session["ws_token"],
        ws_path="/api/vnc/ws",
        vnc_password=console["ticket"],
        expires_in=session["expires_in"],
        console_type=console["console_type"],
        **session_guest_fields(service),
    )


@router.get("/{service_id}/vm/vnc-popup", responses=COMMON_ERROR_RESPONSES)
async def admin_vm_vnc_popup(
    service_id: int,
    type: Optional[str] = None,
    *,
    auth: AdminDep,
    db: DbDep,
):
    """Mint a one-time console launch ticket and redirect to ``/vnc?t=...``.

    Meant as the target of ``window.open(...)`` (a real browser popup,
    authenticated by the admin's own session cookie) rather than a fetch --
    a real top-level window gives the console its own clipboard/focus
    context, unlike the in-page modal. Placement/power-state are validated
    by the redeem step on the ``/vnc`` page itself (same as the WHMCS popup
    flow in ``whmcs/.../vnc_open.php``), so this only needs to check the
    service exists and is a VM before minting the ticket. ``type``
    (``"vnc"``/``"serial"``) carries the console type the UI's picker chose,
    if any.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        return RedirectResponse(url=build_relative_error_url("Service not found"), status_code=status.HTTP_302_FOUND)
    if service.service_type != ServiceType.VM:
        return RedirectResponse(url=build_relative_error_url(_MSG_NOT_A_VM_SERVICE), status_code=status.HTTP_302_FOUND)
    token = mint_launch_ticket(service.id, console_type=type)
    logger.info("Admin API: minted VM console popup ticket for service %s", service.id)
    return RedirectResponse(url=build_relative_launch_url(token), status_code=status.HTTP_302_FOUND)


@router.get("/{service_id}/kvm-popup", responses=COMMON_ERROR_RESPONSES)
async def admin_kvm_popup(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """Mint a one-time HTML5 KVM launch ticket and redirect to ``/kvm?t=...``."""
    del auth
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        return RedirectResponse(url=kvm_error_url("Service not found"), status_code=status.HTTP_302_FOUND)
    return kvm_popup_redirect(service_linked_server(db, service))


@router.get("/{service_id}/sol-popup", responses=COMMON_ERROR_RESPONSES)
async def admin_sol_popup(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """Mint a one-time SOL launch ticket and redirect to ``/sol?t=...``."""
    del auth
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        return RedirectResponse(url=sol_error_url("Service not found"), status_code=status.HTTP_302_FOUND)
    return sol_popup_redirect(service_linked_server(db, service))


@router.get("/{service_id}/virtual-media", response_model=VirtualMediaStatusResponse, responses=COMMON_ERROR_RESPONSES)
async def admin_get_virtual_media(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    del auth
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return await perform_status(service_linked_server(db, service))


@router.post("/{service_id}/virtual-media/insert", response_model=VirtualMediaStatusResponse, responses=COMMON_ERROR_RESPONSES)
async def admin_insert_virtual_media(
    service_id: int,
    body: VirtualMediaInsertRequest,
    auth: AdminDep,
    db: DbDep,
):
    del auth
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return await perform_insert(
        db,
        service_linked_server(db, service),
        body.filename,
        boot_once=body.boot_once,
        source="admin_api",
        service_id=service.id,
    )


@router.post("/{service_id}/virtual-media/eject", response_model=VirtualMediaStatusResponse, responses=COMMON_ERROR_RESPONSES)
async def admin_eject_virtual_media(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    del auth
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return await perform_eject(
        db,
        service_linked_server(db, service),
        source="admin_api",
        service_id=service.id,
    )


@router.post("/{service_id}/sol/send", response_model=SolSendResponse, responses=COMMON_ERROR_RESPONSES)
async def admin_sol_send(
    service_id: int,
    body: SolSendRequest,
    auth: AdminDep,
    db: DbDep,
):
    del auth
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return await perform_sol_send(
        db,
        service_linked_server(db, service),
        body,
        source="admin.service",
        service_id=service.id,
    )


@router.post("/{service_id}/vm/destroy", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def admin_destroy_vm_guest(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    plugin, _cid, _node, vmid = await _admin_get_vm_plugin(db, service)
    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="destroy_vm_guest",
        source="admin_api",
        message="Destroying VM guest",
        details={"service_id": service.id, "vmid": vmid},
    )
    # Proxmox cannot destroy a running VM; stop first, then delete.
    try:
        state = await plugin.get_power_state()
    except Exception:
        state = PowerState.UNKNOWN
    if state == PowerState.ON:
        await plugin.power_off(force=False)
        for _ in range(15):
            await asyncio.sleep(1.0)
            try:
                if await plugin.get_power_state() == PowerState.OFF:
                    break
            except Exception:
                continue
        try:
            state = await plugin.get_power_state()
        except Exception:
            state = PowerState.UNKNOWN
        if state != PowerState.OFF:
            await plugin.power_off(force=True)
            for _ in range(15):
                await asyncio.sleep(1.0)
                try:
                    if await plugin.get_power_state() == PowerState.OFF:
                        break
                except Exception:
                    continue

    ok = await plugin.delete_vm({"vmid": int(vmid)})
    if not ok:
        _set_guest_state(db, service, VMGuestState.ERROR, error="VM delete failed")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="VM delete failed")
    _set_guest_state(db, service, VMGuestState.DESTROYED)
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="destroy_vm_guest",
        source="admin_api",
        message="Destroyed VM guest",
        details={"service_id": service.id, "vmid": vmid},
    )
    db.refresh(service)
    return _service_to_admin_response(db, service)


@router.post("/{service_id}/vm/recreate", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
def admin_recreate_vm_guest(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NOT_A_VM_SERVICE)
    if service.vm:
        service.vm.guest_state = VMGuestState.PROVISIONING
        service.vm.guest_last_error = None
        ServiceDAO.update(db, service)
    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="recreate_vm_guest",
        source="admin_api",
        message="Recreating VM guest",
        details={"service_id": service.id},
    )
    try:
        service, _job = provision_vm_service_async(db, service_id)
    except ValueError as exc:
        _set_guest_state(db, service, VMGuestState.ERROR, error=str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Admin VM recreate failed for service %s", service_id)
        _set_guest_state(db, service, VMGuestState.ERROR, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="recreate_vm_guest",
        source="admin_api",
        message="Recreated VM guest",
        details={"service_id": service.id},
    )
    return _service_to_admin_response(db, service)


@router.put("/{service_id}/vm/ssh-keys", responses=COMMON_ERROR_RESPONSES)
async def admin_put_vm_ssh_keys(
    service_id: int,
    body: VmSshKeysBody,
    auth: AdminDep,
    db: DbDep,
):
    """Validate, store SSH public keys, and best-effort apply via guest agent."""
    from app.services.vm_ssh_keys_service import VmSshKeysError, save_and_apply_ssh_public_keys

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NOT_A_VM_SERVICE)
    try:
        return await save_and_apply_ssh_public_keys(db, service, body.ssh_public_keys)
    except VmSshKeysError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/{service_id}/vm/ssh-keys", responses=COMMON_ERROR_RESPONSES)
async def admin_get_vm_ssh_keys(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    from app.services.ssh_public_keys import ssh_key_fields_for_service
    from app.services.vm_ssh_keys_service import list_reinstall_templates_for_service

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NOT_A_VM_SERVICE)
    fields = ssh_key_fields_for_service(db, service)
    return {
        **fields,
        "vm_template_id": service.vm.vm_template_id if service.vm else None,
        "reinstall_templates": list_reinstall_templates_for_service(db, service),
    }


@router.post("/{service_id}/vm/reinstall", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def admin_reinstall_vm_guest(
    service_id: int,
    body: Optional[VmReinstallBody] = None,
    *,
    auth: AdminDep,
    db: DbDep,
):
    """Destroy the guest (if present) then reprovision at the same reserved VMID."""
    from app.services.vm_ssh_keys_service import VmSshKeysError, apply_template_change_for_reinstall

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NOT_A_VM_SERVICE)

    payload = body or VmReinstallBody()
    if payload.vm_template_id is not None or payload.ssh_public_keys is not None:
        try:
            service = apply_template_change_for_reinstall(
                db,
                service,
                vm_template_id=payload.vm_template_id,
                ssh_public_keys=payload.ssh_public_keys,
            )
        except VmSshKeysError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="reinstall_vm_guest",
        source="admin_api",
        message="Reinstalling VM guest (destroy + recreate, same VMID)",
        details={
            "service_id": service.id,
            "vmid": service.vm.proxmox_vmid if service.vm else None,
            "vm_template_id": payload.vm_template_id,
        },
    )
    # Destroy first when a guest exists; ignore missing-guest / already-gone errors.
    try:
        plugin, _cid, _node, _vmid = await _admin_get_vm_plugin(db, service)
        try:
            exists = await plugin.vm_exists()
        except Exception:
            exists = False
        if exists:
            await admin_destroy_vm_guest(service_id, auth, db)
    except HTTPException as exc:
        if exc.status_code not in (
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_502_BAD_GATEWAY,
        ):
            raise
        logger.info("Admin reinstall: destroy skipped/failed for service %s: %s", service_id, exc.detail)

    result = admin_recreate_vm_guest(service_id, auth, db)
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="reinstall_vm_guest",
        source="admin_api",
        message="Queued VM reinstall at reserved VMID",
        details={"service_id": service.id},
    )
    return result


@router.get("/{service_id}/vm/backups", responses=COMMON_ERROR_RESPONSES)
async def admin_list_vm_backups(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    try:
        items, jobs = await list_service_backups_and_jobs(db, service)
    except Exception as exc:
        raise map_backup_error(exc) from exc
    return {"backups": items, "jobs": jobs}


@router.post("/{service_id}/vm/backups", responses=COMMON_ERROR_RESPONSES)
async def admin_create_vm_backup(
    service_id: int,
    body: BackupCreateBody,
    auth: AdminDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    try:
        return await create_client_backup(
            db, service, notes=body.notes, mode=body.mode, wait=body.wait
        )
    except Exception as exc:
        raise map_backup_error(exc) from exc


@router.post("/{service_id}/vm/backups/delete", responses=COMMON_ERROR_RESPONSES)
async def admin_delete_vm_backup(
    service_id: int,
    body: BackupMutateBody,
    auth: AdminDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    try:
        await delete_client_backup(db, service, volid=body.volid, storage=body.storage)
    except Exception as exc:
        raise map_backup_error(exc) from exc
    return {"status": "ok"}


@router.post("/{service_id}/vm/backups/restore", responses=COMMON_ERROR_RESPONSES)
async def admin_restore_vm_backup(
    service_id: int,
    body: BackupMutateBody,
    auth: AdminDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    try:
        result = await restore_service_backup(
            db,
            service,
            volid=body.volid,
            storage=body.storage,
            wait=body.wait,
            start=body.start,
            vm_template_id=getattr(body, "vm_template_id", None),
        )
    except Exception as exc:
        raise map_backup_error(exc) from exc
    if body.wait and service.vm:
        _set_guest_state(db, service, VMGuestState.STOPPED if not body.start else VMGuestState.RUNNING)
        ServiceDAO.update(db, service)
    return result


@router.get("/{service_id}", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def get_service(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """Get service details"""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found",
        )

    return _service_to_admin_response(db, service)


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
async def delete_service_completely(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """Hard-delete a service and all dependent extension rows."""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found",
        )
    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="delete_service_completely",
        source="admin_api",
        message=f"Deleting service '{service.name}'",
        details={"service_id": service.id, "service_type": service.service_type.value if service.service_type else None},
    )
    ServiceDAO.delete(db, service.id)
    return None


@router.put("/{service_id}/owner", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def assign_service_owner(
    service_id: int,
    body: ServiceOwnerAssignBody,
    auth: AdminDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if body.owner_user_id is not None:
        owner = UserDAO.get_by_id(db, body.owner_user_id)
        if owner is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_OWNER_USER_NOT_FOUND)
    service.owner_user_id = body.owner_user_id
    ServiceDAO.update(db, service)
    return _service_to_admin_response(db, service)


@router.put("/{service_id}/permissions", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def assign_service_permissions(
    service_id: int,
    body: ServicePermissionsAssignBody,
    auth: AdminDep,
    db: DbDep,
):
    """Set this service's permission preset and/or sparse per-key overrides
    (the two most-specific layers in the resolution hierarchy — see
    ``app.services.client_permission_resolver``)."""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if body.permission_set_id is not None and PermissionSetDAO.get_by_id(db, body.permission_set_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission set not found")
    service.permission_set_id = body.permission_set_id
    service.permission_overrides = body.permission_overrides
    ServiceDAO.update(db, service)
    return _service_to_admin_response(db, service)


@router.get("/{service_id}/effective-permissions", response_model=Dict[str, bool], responses=COMMON_ERROR_RESPONSES)
async def get_service_effective_permissions(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """Preview the fully-resolved client permission map for this service."""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return resolve_client_permissions(db, service)


class StrategyActionBody(BaseModel):
    params: Dict[str, Any] = Field(default_factory=dict)


@router.get("/{service_id}/actions", responses=COMMON_ERROR_RESPONSES)
async def admin_list_strategy_actions(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    from app.services.strategy_actions import list_actions

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return {"actions": list_actions(db, service, "admin")}


@router.post("/{service_id}/actions/{action_name}", responses=COMMON_ERROR_RESPONSES)
async def admin_run_strategy_action(
    service_id: int,
    action_name: str,
    body: StrategyActionBody,
    auth: AdminDep,
    db: DbDep,
):
    from app.services.strategy_actions import StrategyActionError, run_action

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    try:
        return await run_action(db, service, action_name, body.params, "admin")
    except StrategyActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc) or "Strategy action failed",
        ) from exc


class ReassignVmIpBody(BaseModel):
    allocation_id: int = Field(..., description="Free VM IP pool row id to assign")
    reset_network: bool = Field(
        True,
        description=(
            "After swapping the pool row, run strategy reset_network. "
            "For Linux cloud-init guests this regenerates cloud-init and reboots the VM."
        ),
    )


@router.get("/{service_id}/available-ips", responses=COMMON_ERROR_RESPONSES)
async def admin_list_available_vm_ips(
    service_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """Browse free VM IP pool rows usable on this service's Proxmox cluster."""
    from app.services.vm_ip_reassign import VmIpReassignError, list_available_ips_for_service

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    try:
        return list_available_ips_for_service(db, service)
    except VmIpReassignError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/{service_id}/reassign-ip", responses=COMMON_ERROR_RESPONSES)
async def admin_reassign_vm_ip(
    service_id: int,
    body: ReassignVmIpBody,
    auth: AdminDep,
    db: DbDep,
):
    """Release the current VM IP, claim a free pool row, and optionally reset guest networking."""
    from app.services.vm_ip_reassign import VmIpReassignError, reassign_vm_ip

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    try:
        return await reassign_vm_ip(
            db,
            service,
            allocation_id=body.allocation_id,
            reset_network=body.reset_network,
            source="admin_api",
        )
    except VmIpReassignError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.put("/{service_id}/status", response_model=ServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def update_service_status(
    service_id: int,
    body: ServiceStatusUpdateBody,
    auth: AdminDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    try:
        new_status = ServiceStatus((body.status or "").strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid status. Use active, suspended, terminated, or pending.",
        ) from exc

    old_status = service.status
    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="update_service_status",
        source="admin_api",
        message=f"Updating service status from {old_status.value} to {new_status.value}",
        details={"old_status": old_status.value, "new_status": new_status.value},
    )

    service.status = new_status
    if new_status == ServiceStatus.TERMINATED:
        service.terminated_at = datetime.now(timezone.utc)
        VMIPAllocationDAO.release_for_service(db, service.id)
        IPAMDAO.release_all_for_service(db, service.id, released_by="admin")
        if service.service_type == ServiceType.VM:
            try:
                await purge_client_backups(db, service)
            except Exception:
                logger.exception(
                    "Failed to purge client backups on terminate for service %s", service.id
                )
    else:
        service.terminated_at = None
    ServiceDAO.update(db, service)

    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="update_service_status",
        source="admin_api",
        message=f"Updated service status to {new_status.value}",
        details={"old_status": old_status.value, "new_status": new_status.value},
    )
    return _service_to_admin_response(db, service)


def _create_admin_vm_core(
    db: Session,
    body: AdminVmServiceCreate,
    background_tasks: Optional[BackgroundTasks] = None,
) -> Service:
    """Shared create logic for POST /vm and legacy internal-test-vm."""
    if ServiceDAO.get_by_name(db, body.name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A service with this name already exists",
        )

    if body.proxmox_cluster_id is not None:
        if ProxmoxInventoryDAO.get_cluster(db, body.proxmox_cluster_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Proxmox cluster not found",
            )

    owner_uid = body.owner_user_id
    owner_user = UserDAO.get_by_id(db, owner_uid) if owner_uid is not None else None
    if owner_uid is not None and owner_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_OWNER_USER_NOT_FOUND,
        )
    prov = (
        ProvisioningSource.BILLING
        if owner_user is not None and owner_user.billing_integration_id
        else ProvisioningSource.INTERNAL
    )

    try:
        product_snapshot, effective_os_code = build_product_snapshot(
            db,
            body.product_code,
            None,
            ServiceType.VM,
            vm_template_id=body.vm_template_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    node = (body.proxmox_node_name or "").strip() or None
    vmid = body.proxmox_vmid
    if vmid is not None and body.proxmox_cluster_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="proxmox_vmid requires proxmox_cluster_id",
        )

    service = ServiceDAO.create_vm(
        db,
        name=body.name,
        owner_user_id=owner_uid,
        external_service_id=body.external_service_id,
        status=ServiceStatus.PENDING,
        description=body.description,
        config=body.service_config or {},
        product_code=body.product_code,
        os_code=effective_os_code,
        product_snapshot=product_snapshot,
        provisioning_source=prov,
        proxmox_cluster_id=body.proxmox_cluster_id,
        proxmox_node_name=node,
        proxmox_vmid=vmid,
        vm_template_id=body.vm_template_id,
    )
    db.refresh(service)

    if service.vm and body.proxmox_cluster_id is not None:
        try:
            reserved_vmid = reserve_vmid_for_service(
                db,
                cluster_id=body.proxmox_cluster_id,
                service_id=service.id,
                requested_vmid=vmid,
            )
        except ValueError as exc:
            ServiceDAO.delete(db, service.id)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        service.vm.proxmox_vmid = int(reserved_vmid)
        db.commit()
        db.refresh(service)

    allocation = VMIPAllocationDAO.assign_next_free_to_service(
        db,
        service_id=service.id,
        proxmox_cluster_id=body.proxmox_cluster_id,
    )
    if allocation is None:
        ServiceDAO.delete(db, service.id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No free VM IP address available to allocate. Add enabled addresses under VM IP allocations. "
                "For services without Proxmox cluster placement, only pool rows with no cluster restriction apply; "
                "when a cluster is set, rows restricted to that cluster (or unrestricted rows) may be used."
            ),
        )

    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create_admin_vm",
        source="admin_api",
        message=f"Creating VM service '{body.name}' (pending)",
        details={
            "proxmox_cluster_id": body.proxmox_cluster_id,
            "proxmox_node_name": node,
            "proxmox_vmid": vmid,
            "provisioning_source": prov.value,
            "vm_ip_allocation_id": allocation.id,
            "vm_ip_address": allocation.ip_address,
        },
    )

    vm_plan = VMProvisioningService.plan_provisioning(
        db=db,
        service_id=service.id,
        product_code=body.product_code,
        os_code=None,
        vm_template_id=body.vm_template_id,
        context={"service_id": service.id, "vm_ip_allocation_id": allocation.id},
    )
    service.config = {
        **(service.config or {}),
        "vm_ip_allocation_id": allocation.id,
        "vm_ip_address": allocation.ip_address,
        "vm_plan": vm_plan,
    }
    ServiceDAO.update(db, service)

    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create_admin_vm",
        source="admin_api",
        message=f"Created pending VM service '{body.name}'",
        details={
            "service_id": service.id,
            "vm_ip_allocation_id": allocation.id,
            "vm_ip_address": allocation.ip_address,
        },
    )

    if body.auto_provision and background_tasks is not None:
        try:
            schedule_vm_auto_provision(db, service, background_tasks)
        except ValueError as exc:
            # Placement could not be resolved. Keep the pending service (IP is
            # already allocated) so it can be fixed and provisioned manually.
            _set_guest_state(db, service, VMGuestState.ERROR, error=str(exc))
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        logger.info("Admin API: auto-provisioning queued for VM service %s", service.id)

    return service


@router.post("/vm", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
async def create_vm_service_admin(
    body: AdminVmServiceCreate,
    background_tasks: BackgroundTasks,
    auth: AdminDep,
    db: DbDep,
):
    """
    Create a VM service (``services`` + ``service_vm``).

    Catalog product + VM template are required. When ``auto_provision`` is true (default),
    placement is resolved (auto-placed from inventory if node omitted), a VMID is reserved,
    and the guest is provisioned in the background (poll ``vm_guest_state`` /
    ``config.vm_provision.status``). Set ``auto_provision`` false to create a pending service
    for the manual two-phase flow.
    """
    service = _create_admin_vm_core(db, body, background_tasks)
    logger.info("Admin API: created VM service %s", service.id)
    return _service_to_admin_response(db, service)


@router.post("/internal-test-vm", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
async def create_internal_test_vm_service(
    body: InternalTestVMServiceCreate,
    auth: AdminDep,
    db: DbDep,
):
    """
    Legacy: same as ``POST /admin/services/vm`` but Proxmox placement was required.
    Prefer ``POST /admin/services/vm`` with optional placement.
    """
    admin_body = AdminVmServiceCreate(
        name=body.name,
        product_code=body.product_code,
        vm_template_id=body.vm_template_id,
        description=body.description,
        service_config=body.service_config,
        proxmox_cluster_id=body.proxmox_cluster_id,
        proxmox_node_name=body.proxmox_node_name,
        proxmox_vmid=body.proxmox_vmid,
        auto_provision=False,
    )
    service = _create_admin_vm_core(db, admin_body)
    logger.info("Admin API: created internal test VM service %s (legacy path)", service.id)
    return _service_to_admin_response(db, service)


@router.post("/http-proxy", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
async def create_http_proxy_service_admin(
    body: AdminHttpProxyServiceCreate,
    auth: AdminDep,
    db: DbDep,
):
    """
    Create an http_proxy service with no linked rack Server; IP(s) are
    auto-assigned from IPAM immediately (status becomes ``active`` as soon
    as at least one IP is assigned, ``pending`` if the pool is exhausted).
    """
    if ServiceDAO.get_by_name(db, body.name):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A service with this name already exists")

    owner_uid = body.owner_user_id
    owner_user = UserDAO.get_by_id(db, owner_uid) if owner_uid is not None else None
    if owner_uid is not None and owner_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_OWNER_USER_NOT_FOUND)
    prov = (
        ProvisioningSource.BILLING
        if owner_user is not None and owner_user.billing_integration_id
        else ProvisioningSource.INTERNAL
    )

    product_snapshot: Dict[str, Any] = {}
    if body.product_code:
        try:
            product_snapshot, _os_code = build_product_snapshot(
                db, body.product_code, None, ServiceType.HTTP_PROXY
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    service = ServiceDAO.create_bare_metal(
        db,
        name=body.name,
        server_id=None,
        owner_user_id=owner_uid,
        external_service_id=body.external_service_id,
        service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.PENDING,
        description=body.description,
        config=body.service_config or {},
        product_code=body.product_code,
        product_snapshot=product_snapshot,
        provisioning_source=prov,
    )
    db.refresh(service)

    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create_admin_http_proxy",
        source="admin_api",
        message=f"Creating proxy service '{body.name}' (pending)",
        details={"provisioning_source": prov.value},
    )

    ip_req = resolve_proxy_ip_request(
        product_snapshot.get("effective_specs"),
        override_ip_count=body.ip_count,
        override_subnet_id=body.subnet_id,
        override_strategy=body.allocation_strategy,
        override_subnet_group_id=body.subnet_group_id,
    )
    try:
        assignments = auto_assign_proxy_ips(
            db,
            service,
            ip_count=ip_req.ip_count,
            subnet_id=ip_req.subnet_id,
            subnet_group_id=ip_req.subnet_group_id,
            strategy=ip_req.strategy,
            assigned_by="admin",
        )
    except Exception as exc:
        ServiceDAO.delete(db, service.id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    ServiceDAO.update(db, service)

    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create_admin_http_proxy",
        source="admin_api",
        message=f"Created proxy service '{body.name}' ({len(assignments)}/{ip_req.ip_count} IP(s) assigned)",
        details={"assigned_ip_count": len(assignments), "requested_ip_count": ip_req.ip_count},
    )
    logger.info(
        "Admin API: created http_proxy service %s (%s/%s IPs)",
        service.id,
        len(assignments),
        ip_req.ip_count,
    )
    return _service_to_admin_response(db, service)
