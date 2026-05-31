"""
Admin API endpoints for managing services and external users.

These endpoints are for admin users to view and manage services
and external users created via billing integrations.
"""
from fastapi import APIRouter, Depends, HTTPException, status
import asyncio
from sqlalchemy.orm import Session
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao.service_dao import ServiceDAO
from app.dao.external_user_dao import ExternalUserDAO
from app.dao.user_dao import UserDAO
from app.dao.user_external_identity_link_dao import UserExternalIdentityLinkDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.models.service import Service, ServiceStatus, ServiceType, ProvisioningSource
from app.models.service_bare_metal import ServiceBareMetal
from app.services.service_product_snapshot import build_product_snapshot
from app.services.vm_provisioning_service import VMProvisioningService
from app.services.vmid_allocator import reserve_vmid_for_service
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_success,
)
from app.services.service_resource import service_linked_server, service_server_id_for_response, vm_placement
from app.models.server_activity import ServerActivityEventType
from app.services.vm_strategy_executor import run_provision_vm_service, resolve_vm_strategy_name_for_service
from app.services.proxmox_placement import cluster_to_proxmox_plugin_config
from app.plugins.registry import get_registry
from app.plugins.base import PowerState
from app.models.service_vm import VMGuestState
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

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
    status: str
    description: Optional[str] = None
    config: Optional[dict] = None
    created_at: str
    updated_at: str
    terminated_at: Optional[str] = None

    class Config:
        from_attributes = True


class InternalTestVMServiceCreate(BaseModel):
    """Create a VM service without billing / external user (lab or QA)."""

    name: str = Field(..., description="Unique service name")
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

    name: str = Field(..., description="Unique service name")
    product_code: str
    vm_template_id: int = Field(..., description="Catalog VM template id linked to product")
    description: Optional[str] = None
    service_config: Optional[Dict[str, Any]] = None
    external_user_id: Optional[int] = Field(
        None,
        description="external_users.id — if set, provisioning_source is billing (WHMCS-style owner)",
    )
    owner_user_id: Optional[int] = Field(
        None,
        description="users.id canonical owner in RackFlow (client-facing ownership)",
    )
    external_service_id: Optional[str] = Field(None, description="Optional external line-item id (e.g. WHMCS service id)")
    proxmox_cluster_id: Optional[int] = None
    proxmox_node_name: Optional[str] = None
    proxmox_vmid: Optional[int] = None


class ServiceOwnerAssignBody(BaseModel):
    owner_user_id: Optional[int] = Field(
        None,
        description="users.id canonical owner; null unassigns owner",
    )


class UserExternalIdentityLinkCreate(BaseModel):
    user_id: int
    external_user_id: int


class UserExternalIdentityLinkResponse(BaseModel):
    id: int
    user_id: int
    username: str
    email: str
    external_user_id: int
    external_user_external_id: str
    integration_id: int
    integration_name: str
    created_at: str


class VmPowerActionBody(BaseModel):
    action: str = Field(..., description="on | off | reboot | reset")


class VmPlacementUpdateBody(BaseModel):
    proxmox_cluster_id: int
    proxmox_node_name: str
    proxmox_vmid: Optional[int] = None


class ServiceStatusUpdateBody(BaseModel):
    status: str = Field(..., description="active | suspended | terminated | pending")


def _service_to_admin_response(db: Session, service) -> ServiceResponse:
    server = service_linked_server(db, service)
    eu = service.external_user
    owner = service.owner_user
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
    vm_strategy_name = None
    if service.service_type == ServiceType.VM:
        vm_strategy_name = resolve_vm_strategy_name_for_service(db, service)
    return ServiceResponse(
        id=service.id,
        name=service.name,
        external_service_id=service.external_service_id,
        owner_user_id=service.owner_user_id,
        owner_username=owner.username if owner else None,
        owner_email=owner.email if owner else None,
        server_id=service_server_id_for_response(service),
        server_name=server.name if server else "",
        external_user_id=service.external_user_id,
        external_user_external_id=eu.external_user_id if eu else None,
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
        status=service.status.value,
        description=service.description,
        config=service.config,
        created_at=service.created_at.isoformat(),
        updated_at=service.updated_at.isoformat(),
        terminated_at=service.terminated_at.isoformat() if service.terminated_at else None,
    )


def _identity_link_to_response(link) -> UserExternalIdentityLinkResponse:
    user = link.user
    ext = link.external_user
    integration = ext.integration if ext else None
    return UserExternalIdentityLinkResponse(
        id=link.id,
        user_id=link.user_id,
        username=user.username if user else "",
        email=user.email if user else "",
        external_user_id=link.external_user_id,
        external_user_external_id=ext.external_user_id if ext else "",
        integration_id=integration.id if integration else 0,
        integration_name=integration.name if integration else "",
        created_at=link.created_at.isoformat(),
    )


def _admin_get_vm_plugin(db: Session, service: Service):
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a VM service")
    cid, node, vmid = vm_placement(service)
    if cid is None or not (node and str(node).strip()) or vmid is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="VM service is missing Proxmox placement (proxmox_cluster_id, proxmox_node_name, proxmox_vmid)",
        )
    cluster = ProxmoxInventoryDAO.get_cluster(db, cid)
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown proxmox_cluster_id {cid}")
    try:
        plugin_config = cluster_to_proxmox_plugin_config(cluster, str(node).strip(), int(vmid))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    plugin = get_registry().get_plugin("proxmox", plugin_config)
    return plugin, int(vmid)


def _set_guest_state(db: Session, service: Service, state: VMGuestState, error: Optional[str] = None) -> None:
    if not service.vm:
        return
    service.vm.guest_state = state
    service.vm.guest_last_error = error
    ServiceDAO.update(db, service)


@router.get("/external-users", response_model=List[ExternalUserResponse])
async def list_external_users(
    integration_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all external users"""
    from app.models.external_user import ExternalUser

    query = db.query(ExternalUser)
    if integration_id:
        query = query.filter(ExternalUser.integration_id == integration_id)

    external_users = query.order_by(ExternalUser.created_at.desc()).offset(skip).limit(limit).all()

    result = []
    for eu in external_users:
        services = ServiceDAO.get_by_external_user(db, eu.id)

        result.append(
            ExternalUserResponse(
                id=eu.id,
                integration_id=eu.integration_id,
                integration_name=eu.integration.name,
                external_user_id=eu.external_user_id,
                external_username=eu.external_username,
                external_email=eu.external_email,
                created_at=eu.created_at.isoformat(),
                updated_at=eu.updated_at.isoformat(),
                service_count=len(services),
            )
        )

    return result


@router.get("/external-users/{external_user_id}", response_model=ExternalUserResponse)
async def get_external_user(
    external_user_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get external user details"""
    external_user = ExternalUserDAO.get_by_id(db, external_user_id)
    if not external_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="External user not found",
        )

    services = ServiceDAO.get_by_external_user(db, external_user.id)

    return ExternalUserResponse(
        id=external_user.id,
        integration_id=external_user.integration_id,
        integration_name=external_user.integration.name,
        external_user_id=external_user.external_user_id,
        external_username=external_user.external_username,
        external_email=external_user.external_email,
        created_at=external_user.created_at.isoformat(),
        updated_at=external_user.updated_at.isoformat(),
        service_count=len(services),
    )


@router.get("", response_model=List[ServiceResponse])
async def list_services(
    status_filter: Optional[str] = None,
    external_user_id: Optional[int] = None,
    owner_user_id: Optional[int] = None,
    server_id: Optional[int] = None,
    provisioning_source: Optional[str] = None,
    service_type: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all services"""
    query = db.query(Service)

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
        query = query.filter(Service.external_user_id == external_user_id)

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


@router.get("/unassigned", response_model=List[ServiceResponse])
async def list_unassigned_services(
    skip: int = 0,
    limit: int = 100,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
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


@router.post("/backfill-owners")
async def backfill_service_owners_from_identity_links(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    links = UserExternalIdentityLinkDAO.list_all(db)
    ext_to_user = {link.external_user_id: link.user_id for link in links}
    if not ext_to_user:
        return {"updated": 0}
    candidates = (
        db.query(Service)
        .filter(Service.owner_user_id.is_(None), Service.external_user_id.isnot(None))
        .all()
    )
    updated = 0
    for service in candidates:
        owner = ext_to_user.get(service.external_user_id)
        if owner:
            service.owner_user_id = owner
            updated += 1
    if updated:
        db.commit()
    return {"updated": updated}


@router.get("/vm", response_model=List[ServiceResponse])
async def list_vm_services_admin(
    status_filter: Optional[str] = None,
    owner_user_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
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


@router.get("/bare-metal", response_model=List[ServiceResponse])
async def list_bare_metal_services_admin(
    status_filter: Optional[str] = None,
    owner_user_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
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


@router.get("/vm/{service_id}", response_model=ServiceResponse)
async def get_vm_service_admin(
    service_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM service not found")
    return _service_to_admin_response(db, service)


@router.get("/bare-metal/{service_id}", response_model=ServiceResponse)
async def get_bare_metal_service_admin(
    service_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.service_type == ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bare metal service not found")
    return _service_to_admin_response(db, service)


@router.post("/{service_id}/provision-vm", response_model=ServiceResponse)
def admin_provision_vm_service(
    service_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Clone the catalog template on Proxmox to this service's vmid, apply RAM/CPU, then:

    - **cloudinit_clone**: set Proxmox ``ipconfig0`` from the linked VM IP pool row and start the VM.
    - **guest_agent**: apply sizing and start (no cloud-init network).

    Requires synced inventory (template name = catalog ``proxmox_template_name`` on the target node).
    """
    try:
        service = run_provision_vm_service(db, service_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Admin VM provision failed for service %s", service_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    return _service_to_admin_response(db, service)


@router.put("/{service_id}/vm/placement", response_model=ServiceResponse)
async def admin_update_vm_placement(
    service_id: int,
    body: VmPlacementUpdateBody,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.service_type != ServiceType.VM or not service.vm:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM service not found")
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
    try:
        reserved_vmid = reserve_vmid_for_service(
            db,
            cluster_id=body.proxmox_cluster_id,
            service_id=service.id,
            requested_vmid=requested,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    service.vm.proxmox_cluster_id = body.proxmox_cluster_id
    service.vm.proxmox_node_name = (body.proxmox_node_name or "").strip()
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


@router.post("/{service_id}/vm/power", response_model=ServiceResponse)
async def admin_vm_power_action(
    service_id: int,
    body: VmPowerActionBody,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    plugin, _vmid = _admin_get_vm_plugin(db, service)
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


@router.post("/{service_id}/vm/destroy", response_model=ServiceResponse)
async def admin_destroy_vm_guest(
    service_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    plugin, vmid = _admin_get_vm_plugin(db, service)
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


@router.post("/{service_id}/vm/recreate", response_model=ServiceResponse)
def admin_recreate_vm_guest(
    service_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a VM service")
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
        service = run_provision_vm_service(db, service_id)
    except ValueError as exc:
        _set_guest_state(db, service, VMGuestState.ERROR, error=str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except TimeoutError as exc:
        _set_guest_state(db, service, VMGuestState.ERROR, error=str(exc))
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from exc
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


@router.get("/{service_id}", response_model=ServiceResponse)
async def get_service(
    service_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get service details"""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found",
        )

    return _service_to_admin_response(db, service)


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service_completely(
    service_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
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


@router.put("/{service_id}/owner", response_model=ServiceResponse)
async def assign_service_owner(
    service_id: int,
    body: ServiceOwnerAssignBody,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if body.owner_user_id is not None:
        owner = UserDAO.get_by_id(db, body.owner_user_id)
        if owner is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner user not found")
    service.owner_user_id = body.owner_user_id
    ServiceDAO.update(db, service)
    return _service_to_admin_response(db, service)


@router.put("/{service_id}/status", response_model=ServiceResponse)
async def update_service_status(
    service_id: int,
    body: ServiceStatusUpdateBody,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
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


@router.get("/external-user-links", response_model=List[UserExternalIdentityLinkResponse])
async def list_identity_links(
    user_id: Optional[int] = None,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if user_id is not None:
        links = UserExternalIdentityLinkDAO.list_for_user(db, user_id)
    else:
        links = UserExternalIdentityLinkDAO.list_all(db)
    return [_identity_link_to_response(link) for link in links]


@router.post("/external-user-links", response_model=UserExternalIdentityLinkResponse, status_code=status.HTTP_201_CREATED)
async def create_identity_link(
    body: UserExternalIdentityLinkCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if UserDAO.get_by_id(db, body.user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    ext_user = ExternalUserDAO.get_by_id(db, body.external_user_id)
    if ext_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="External user not found")
    existing = UserExternalIdentityLinkDAO.get_by_external_user_id(db, body.external_user_id)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="External user is already linked")
    try:
        link = UserExternalIdentityLinkDAO.create(
            db,
            user_id=body.user_id,
            external_user_id=body.external_user_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    updated = (
        db.query(Service)
        .filter(Service.external_user_id == body.external_user_id, Service.owner_user_id.is_(None))
        .update({"owner_user_id": body.user_id}, synchronize_session=False)
    )
    if updated:
        db.commit()
    db.refresh(link)
    return _identity_link_to_response(link)


@router.delete("/external-user-links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_identity_link(
    link_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not UserExternalIdentityLinkDAO.delete(db, link_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Identity link not found")
    return None


def _create_admin_vm_core(db: Session, body: AdminVmServiceCreate) -> Service:
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

    ext_uid = body.external_user_id
    owner_uid = body.owner_user_id
    if owner_uid is not None and UserDAO.get_by_id(db, owner_uid) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner user not found",
        )
    if ext_uid is not None:
        if ExternalUserDAO.get_by_id(db, ext_uid) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="External user not found",
            )
        prov = ProvisioningSource.BILLING
    else:
        prov = ProvisioningSource.INTERNAL

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
        external_user_id=ext_uid,
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
    return service


@router.post("/vm", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_vm_service_admin(
    body: AdminVmServiceCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Create a **pending** VM service (``services`` + ``service_vm``).

    Catalog product + VM template are required. Proxmox cluster/node/vmid are optional until placement exists.
    Set ``external_user_id`` to attach a billing integration user; otherwise the service is internal/lab.
    """
    service = _create_admin_vm_core(db, body)
    logger.info("Admin API: created VM service %s", service.id)
    return _service_to_admin_response(db, service)


@router.post("/internal-test-vm", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_internal_test_vm_service(
    body: InternalTestVMServiceCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
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
    )
    service = _create_admin_vm_core(db, admin_body)
    logger.info("Admin API: created internal test VM service %s (legacy path)", service.id)
    return _service_to_admin_response(db, service)
