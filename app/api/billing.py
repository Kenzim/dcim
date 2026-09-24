"""
Billing API endpoints for external billing systems.

These endpoints are authenticated via API keys and provide standard operations
for billing systems to manage services (which link to servers).

The integration instance is automatically derived from the API key used for authentication.
All routes are generic and work for any integration type (WHMCS, custom, etc.).
"""
import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, or_
from sqlalchemy.orm import Session, aliased
from sqlalchemy.orm.attributes import flag_modified
from typing import Annotated, Any, List, Optional
from datetime import datetime, timezone
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.core.billing_auth import get_billing_integration
from app.models.billing_integration import BillingIntegration
from app.models.server import Server
from app.models.service import Service, ServiceStatus, ServiceType, ProvisioningSource
from app.schemas.billing import (
    BillingBareMetalServiceCreate,
    BillingVmServiceCreate,
    BillingRegisterService,
    BillingLinkService,
    BillingAdoptVmService,
    BillingVmPlacementUpdate,
    BillingServiceResponse,
    BillingServiceDetailResponse,
    BillingServiceLookupItem,
    PowerAction,
    SuspendAction,
    ServerUsage,
    ServiceActionRunScript,
    ServiceActionReinstallOS,
)
from app.models.user import User
from app.models.service_bare_metal import ServiceBareMetal
from app.models.service_vm import ServiceVm
from app.services.vmid_allocator import reserve_vmid_aligned_with_proxmox
from app.dao.server_dao import ServerDAO
from app.dao.server_group_dao import ServerGroupDAO
from app.utils.shell_escape import shell_escape_double_quoted
from app.dao.disk_dao import DiskDAO
from app.dao.network_port_dao import NetworkPortDAO
from app.plugins.registry import get_registry
from app.dao.user_dao import UserDAO
from app.dao.service_dao import ServiceDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.dao.ipam_dao import IPAMDAO
from app.services.proxy_provisioning import assignment_payload
from app.services.proxy_credentials import generate_proxy_password, generate_proxy_username
from app.dao.installation_task_dao import InstallationTaskDAO
from app.dao.script_dao import ScriptDAO
from app.dao.boot_task_dao import BootTaskDAO
from app.dao.product_catalog_dao import ProductDAO
from app.dao.vm_config_dao import ProductVMConfigDAO
from app.models.boot_task import BootTask, BootType, BootTaskStatus
from app.plugins.base import PowerState
from app.services.os_template_service import (
    PasswordGenerateConfig,
    get_template_service,
    generate_parameter_password,
)
from app.services.virtual_media.iso_catalog import catalog_for_billing
from app.services.temp_os_service import get_temp_os_service
from app.services.download_token_service import get_download_token_service, template_file_scope
from app.services.ipmi_ticket_service import build_launch_payload, IPMIProxyUnavailable
from app.services.ipmi_kvm import kvm_ready
from app.services.ipmi_kvm_ticket_service import (
    IpmiKvmTicketUnavailable,
    build_launch_url as build_kvm_launch_url,
    mint_launch_ticket as mint_kvm_launch_ticket,
)
from app.services.sol import sol_ready
from app.services.virtual_media import virtual_media_ready
from app.api.sol import perform_sol_send
from app.api.virtual_media import perform_eject, perform_insert, perform_status
from app.services.sol.ticket_service import (
    SolTicketUnavailable,
    build_launch_url as build_sol_launch_url,
    mint_launch_ticket as mint_sol_launch_ticket,
)
from app.schemas.sol import SolSendRequest, SolSendResponse, SolTicketResponse
from app.schemas.virtual_media import VirtualMediaInsertRequest, VirtualMediaStatusResponse
from app.services.vm_vnc_ticket_service import mint_launch_ticket, build_launch_url, VmVncUnavailable
from app.schemas.vm_vnc import VmVncTicketResponse
from app.schemas.ipmi_kvm import IpmiKvmTicketResponse
from app.services.client_portal_service import ensure_user_for_billing_identity, mint_sso_ticket
from app.services.client_permission_resolver import (
    resolve_client_permissions,
    require_client_permission,
)
from app.core.client_permissions import PermissionKey
from app.core.config import settings
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_success,
    log_server_activity_failure,
)
from app.models.server_activity import ServerActivityEventType
from app.api.server_interaction import _get_base_url_for_pxe_ip, _inject_script_url_param
from app.services.service_product_snapshot import build_product_snapshot
from app.services.service_resource import (
    service_linked_server,
    service_server_id_for_response,
    vm_placement,
)
from app.services.proxmox_placement import (
    ProxmoxPlacementError,
    resolve_proxmox_plugin_for_service,
)
from app.services.provisioning import (
    ProvisioningActor,
    ProvisioningError,
    ProvisioningService,
)
from app.services.provisioning.adapters import from_billing_bare_metal, from_billing_vm
from app.services.provisioning.bare_metal import (
    determine_template_for_group as _bm_determine_template_for_group,
    select_free_server_in_group as _bm_select_free_server_in_group,
)
from app.services.service_lifecycle import (
    ServiceLifecycle,
    ServiceLifecycleError,
)
from app.services.strategy_actions import (
    StrategyActionError,
    list_actions,
    run_action,
)
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
import logging
import os

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

_NO_LINKED_SERVER_MSG = "Service has no linked server"
_SERVICE_NOT_FOUND_MSG = "Service not found"
_AUDIENCE_ADMIN_OR_CLIENT_MSG = "audience must be admin or client"
_NOT_VM_SERVICE_MSG = "Not a VM service"
_BILLING_SERVICE_LITERAL = "billing.service"


BillingIntegrationDep = Annotated[BillingIntegration, Depends(get_billing_integration)]
DbDep = Annotated[Session, Depends(get_db)]


def _assert_billing_owned_service(service: Service, integration: BillingIntegration) -> None:
    """Reject internal-only services and services owned by another integration (404)."""
    owner = service.owner_user
    if owner is None or owner.billing_integration_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG,
        )
    if owner.billing_integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG,
        )


def _assert_linkable_service(service: Service, integration: BillingIntegration) -> None:
    """
    Allow linking services owned by this integration, or unowned/internal services
    that an admin is claiming for billing.
    """
    owner = service.owner_user
    if owner is None or owner.billing_integration_id is None:
        return
    if owner.billing_integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG,
        )


def _lookup_item_from_service(db: Session, service: Service) -> BillingServiceLookupItem:
    resp = _billing_service_response(db, service)
    server = service_linked_server(db, service)
    return BillingServiceLookupItem(
        id=resp.id,
        name=resp.name,
        external_service_id=resp.external_service_id,
        service_type=resp.service_type,
        status=resp.status,
        proxmox_cluster_id=resp.proxmox_cluster_id,
        proxmox_node_name=resp.proxmox_node_name,
        proxmox_vmid=resp.proxmox_vmid,
        server_ip=resp.server_ip,
        server_name=server.name if server else None,
        source="rackflow",
    )


def _validate_optional_proxmox_cluster(db: Session, cid: Optional[int]) -> None:
    if cid is not None and ProxmoxInventoryDAO.get_cluster(db, cid) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown proxmox_cluster_id {cid}",
        )


def _billing_proxy_assignments(db: Session, service: Service):
    if service.service_type != ServiceType.HTTP_PROXY:
        return None
    return [assignment_payload(a) for a in IPAMDAO.get_assignment_by_service(db, service.id)]


def _billing_vm_ip_fields(service: Service) -> tuple[Optional[int], Optional[str]]:
    if not service.vm:
        return None, None
    vm_ip_allocation_id = service.vm.vm_ip_allocation_id
    if service.vm.vm_ip_allocation:
        return vm_ip_allocation_id, service.vm.vm_ip_allocation.ip_address
    if not service.config:
        return vm_ip_allocation_id, None
    cfg = service.config or {}
    return (
        vm_ip_allocation_id or cfg.get("vm_ip_allocation_id"),
        cfg.get("vm_ip_address"),
    )


def _billing_external_user_id(service: Service) -> Optional[int]:
    owner = service.owner_user
    if owner and owner.billing_integration_id:
        return service.owner_user_id
    return None


def _billing_service_response(db: Session, service: Service) -> BillingServiceResponse:
    server = service_linked_server(db, service)
    cid, node, vmid = vm_placement(service)
    proxy_assignments = _billing_proxy_assignments(db, service)
    src = service.provisioning_source or ProvisioningSource.BILLING
    vm_tid = service.vm.vm_template_id if service.vm else None
    vm_ip_allocation_id, vm_ip_address = _billing_vm_ip_fields(service)
    return BillingServiceResponse(
        id=service.id,
        name=service.name,
        external_service_id=service.external_service_id,
        service_type=service.service_type.value if service.service_type else None,
        product_code=service.product_code,
        os_code=service.os_code,
        vm_template_id=vm_tid,
        server_id=service_server_id_for_response(service),
        external_user_id=_billing_external_user_id(service),
        provisioning_source=src.value if hasattr(src, "value") else str(src),
        proxmox_cluster_id=cid,
        proxmox_node_name=node,
        proxmox_vmid=vmid,
        vm_ip_allocation_id=vm_ip_allocation_id,
        vm_ip_address=vm_ip_address,
        vm_guest_state=service.vm.guest_state.value if service.vm and service.vm.guest_state else None,
        status=service.status.value,
        description=service.description,
        config=service.config,
        server_ip=server.server_ip if server else None,
        credentials=server.credentials if server else None,
        proxy_assignments=proxy_assignments,
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


async def _billing_get_plugin_instance(db: Session, service: Service):
    """
    Return (plugin_instance, server_or_none). For VM services server is None; plugin is Proxmox.

    VM placement's cached node is treated as a hint: if it's missing or
    stale, this searches the Proxmox cluster for the VMID's current node and
    updates the cache before erroring.
    """
    if service.service_type == ServiceType.VM:
        try:
            inst, _cid, _node, _vmid = await resolve_proxmox_plugin_for_service(db, service)
        except ProxmoxPlacementError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return inst, None
    server = service_linked_server(db, service)
    if not server:
        if service.service_type == ServiceType.HTTP_PROXY:
            # Server-less proxy (provisioned purely from the IP pool): power
            # and IPMI are not applicable, not a data-integrity error.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This proxy service has no linked server; power/IPMI control is not applicable",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_NO_LINKED_SERVER_MSG,
        )
    registry = get_registry()
    inst = registry.get_plugin(server.plugin_name, server.plugin_config)
    return inst, server


def _power_permission_key(service: Service) -> str:
    """The client permission key gating power actions for this service type."""
    return PermissionKey.VM_POWER if service.service_type == ServiceType.VM else PermissionKey.BMS_POWER


async def _ensure_service_powered_off(db: Session, service: Service) -> None:
    """
    Force-stop the linked machine for billing lifecycle (suspend).

    Skips http_proxy and VMs missing Proxmox placement. Already-off is success.
    Raises HTTPException on failure so the caller must not mark the service suspended.
    """
    try:
        await ServiceLifecycle(
            plugin_resolver=_billing_get_plugin_instance
        ).ensure_powered_off(db, service)
    except ServiceLifecycleError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
        ) from exc


def _billing_activity_log_kw(db: Session, service: Service) -> dict:
    """Pass as ``log_server_activity_*(db, **kw, ...)`` — exactly one of server_id / service_id."""
    srv = service_linked_server(db, service)
    if srv:
        return {"server_id": srv.id}
    return {"service_id": service.id}


@router.get("/server-by-ip", responses=COMMON_ERROR_RESPONSES)
async def get_server_by_ip(
    ip: str,
    *,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    Look up a RackFlow server by its primary IP address.
    Used when linking an existing (already deployed) server to a billing service.
    """
    if not ip or not ip.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query parameter 'ip' is required"
        )
    server = ServerDAO.get_by_ip(db, ip.strip())
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No server found with IP {ip.strip()}"
        )
    return {
        "id": server.id,
        "name": server.name,
        "server_ip": server.server_ip,
        "description": server.description,
    }


@router.post("/register-service", response_model=BillingServiceResponse, status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
async def register_service(
    data: BillingRegisterService,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    Register an existing RackFlow server as a billing service (no provisioning).
    Creates/finds the external user and links the server to the WHMCS (or other) service.
    Does not create a server or trigger any OS install.
    """
    logger.info(f"Billing API: Registering existing server {data.server_id} as service via integration '{integration.name}'")

    # Idempotent: if a service already exists for this external_service_id and integration, return it
    existing = ServiceDAO.get_by_external_service_id_and_integration(
        db, data.external_service_id, integration.id
    )
    if existing:
        db.refresh(existing)
        server = service_linked_server(db, existing)
        details = {
            "service_id": existing.id,
            "external_service_id": existing.external_service_id,
            "integration_id": integration.id,
            "idempotent": True,
        }
        if server:
            log_server_activity_success(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.SERVICE,
                action="register",
                source="billing_api",
                message="Service registration request reused existing service",
                details=details,
            )
        else:
            log_server_activity_success(
                db,
                service_id=existing.id,
                event_type=ServerActivityEventType.SERVICE,
                action="register",
                source="billing_api",
                message="Service registration request reused existing service",
                details=details,
            )
        return _billing_service_response(db, existing)

    # Validate server exists
    server = ServerDAO.get_by_id(db, data.server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )

    # Server must not already be linked to a non-terminated service
    existing_services = ServiceDAO.get_by_server(db, server.id)
    for svc in existing_services:
        if svc.status != ServiceStatus.TERMINATED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Server is already linked to service '{svc.name}' (ID: {svc.id}). Unlink or terminate that service first."
            )

    # Find or create the billing owner (WHMCS / billing "virtual" owner) and
    # its linked portal account in one step.
    billing_user = ensure_user_for_billing_identity(
        db,
        billing_integration_id=integration.id,
        external_user_id=data.external_user_id,
        external_username=data.external_username,
        external_email=data.external_email,
    )
    logger.info(f"Resolved billing user (ID: {billing_user.id}, external_user_id: {data.external_user_id})")

    name = data.name or f"service-{data.external_service_id}"
    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.SERVICE,
        action="register",
        source="billing_api",
        message=f"Registering service '{name}'",
        details={
            "external_service_id": data.external_service_id,
            "owner_user_id": billing_user.id,
            "integration_id": integration.id,
        },
    )
    service = ServiceDAO.create_bare_metal(
        db,
        name=name,
        server_id=server.id,
        owner_user_id=billing_user.id,
        external_service_id=data.external_service_id,
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.ACTIVE,
        description=None,
        config=None,
    )
    db.refresh(service)
    logger.info(f"Billing API: Registered service '{service.name}' (ID: {service.id}) for server {server.id}")
    log_server_activity_success(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.SERVICE,
        action="register",
        source="billing_api",
        message=f"Registered service '{service.name}'",
        details={
            "service_id": service.id,
            "external_service_id": service.external_service_id,
            "integration_id": integration.id,
        },
    )
    return _billing_service_response(db, service)


def _raise_provisioning(exc: ProvisioningError) -> None:
    raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc


def _select_free_server_in_group(db: Session, group_id: int) -> Server:
    try:
        return _bm_select_free_server_in_group(db, group_id)
    except ProvisioningError as exc:
        _raise_provisioning(exc)
        raise


def _determine_template_for_group(
    db: Session,
    group_id: int,
    explicit_template_id: str | None,
) -> str:
    try:
        return _bm_determine_template_for_group(db, group_id, explicit_template_id)
    except ProvisioningError as exc:
        _raise_provisioning(exc)
        raise


def _os_template_param_field(param: Any, field: str, default=None):
    if isinstance(param, dict):
        return param.get(field, default)
    return getattr(param, field, default)


def _serialize_os_template_parameters(template) -> dict:
    payload = {}
    for name, param in (getattr(template, "parameters", None) or {}).items():
        payload[name] = {
            "type": _os_template_param_field(param, "type"),
            "label": _os_template_param_field(param, "label"),
            "required": bool(_os_template_param_field(param, "required", False)),
            "default": _os_template_param_field(param, "default"),
            "options": _os_template_param_field(param, "options"),
            "help": _os_template_param_field(param, "help"),
        }
    return payload


def _template_accepts_ssh_key(template) -> bool:
    os_type = (getattr(template, "os_type", None) or "").lower()
    params = getattr(template, "parameters", None) or {}
    return os_type == "linux" and "ssh_public_key" in params


def serialize_os_template(template) -> dict:
    return {
        "id": template.id,
        "name": template.name,
        "description": getattr(template, "description", None) or "",
        "os_type": getattr(template, "os_type", None) or "other",
        "user_reinstallable": bool(getattr(template, "user_reinstallable", False)),
        "accepts_ssh_key": _template_accepts_ssh_key(template),
        "parameters": _serialize_os_template_parameters(template),
    }


def resolved_os_templates_for_ids(template_ids: list | None) -> list[dict]:
    """Resolve disk templates for a list of ids; skip ids that are missing on disk."""
    template_service = get_template_service()
    out: list[dict] = []
    for raw_id in template_ids or []:
        tid = str(raw_id).strip()
        if not tid:
            continue
        template = template_service.get_template(tid)
        if template is None:
            continue
        out.append(serialize_os_template(template))
    return out


def _service_server_group(db: Session, service: Service):
    cfg = service.config if isinstance(getattr(service, "config", None), dict) else {}
    raw_id = cfg.get("server_group_id") if cfg else None
    if raw_id is None or raw_id == "":
        return None
    try:
        return ServerGroupDAO.get_by_id(db, int(raw_id))
    except (TypeError, ValueError):
        return None


def _coerce_template_param_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value if item is not None and str(item) != "")
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _as_password_generate_config(gen) -> Optional[PasswordGenerateConfig]:
    if gen is None:
        return None
    if isinstance(gen, PasswordGenerateConfig):
        return gen if gen.enabled else None
    if isinstance(gen, dict):
        if not gen.get("enabled", True):
            return None
        allowed = {"enabled", "length", "charset", "exclude_ambiguous"}
        return PasswordGenerateConfig(**{k: v for k, v in gen.items() if k in allowed})
    if not bool(getattr(gen, "enabled", True)):
        return None
    kwargs = {}
    for key in ("length", "charset", "exclude_ambiguous"):
        if hasattr(gen, key):
            kwargs[key] = getattr(gen, key)
    return PasswordGenerateConfig(**kwargs)


def _apply_ssh_public_key_alias(supplied: dict) -> None:
    if "ssh_public_key" not in supplied and supplied.get("ssh_public_keys") is not None:
        supplied["ssh_public_key"] = _coerce_template_param_value(supplied.get("ssh_public_keys"))


def _password_alias_from_supplied(supplied: dict) -> str:
    for key in ("password", "admin_password"):
        candidate = _coerce_template_param_value(supplied.get(key))
        if candidate:
            return candidate
    return ""


def _resolve_template_param_value(
    name: str,
    param: Any,
    supplied: dict,
    password_alias: str,
) -> tuple[str, bool]:
    """Return (resolved_value, is_missing_required)."""
    param_type = (_os_template_param_field(param, "type") or "text") or "text"
    required = bool(_os_template_param_field(param, "required", False))
    default = _os_template_param_field(param, "default")
    raw = supplied.get(name)
    value = _coerce_template_param_value(raw) if raw is not None else ""
    if value == "" and param_type == "password" and password_alias:
        value = password_alias
    if value == "" and default not in (None,):
        value = _coerce_template_param_value(default)
    if value == "" and param_type == "password":
        gen_cfg = _as_password_generate_config(_os_template_param_field(param, "generate"))
        if gen_cfg is not None:
            value = generate_parameter_password(gen_cfg)
    return value, value == "" and required


def _merge_extra_template_params(resolved: dict, supplied: dict) -> None:
    for key, extra in supplied.items():
        if key in resolved:
            continue
        if isinstance(extra, (list, tuple)):
            resolved[key] = _coerce_template_param_value(extra)
        else:
            resolved[key] = extra


def resolve_template_parameters(template, supplied: dict | None) -> dict:
    """
    Fill missing template.json parameters from aliases, defaults, or generate specs.

    WHMCS typically supplies ``admin_password`` (service password) and
    ``ssh_public_keys``; disk templates may declare ``password`` / ``ssh_public_key``.
    """
    supplied = dict(supplied or {})
    params_def = getattr(template, "parameters", None) or {}
    if not params_def:
        return supplied

    _apply_ssh_public_key_alias(supplied)
    password_alias = _password_alias_from_supplied(supplied)

    resolved: dict = {}
    missing: list[str] = []
    for name, param in params_def.items():
        value, is_missing = _resolve_template_param_value(name, param, supplied, password_alias)
        if is_missing:
            missing.append(name)
        resolved[name] = value

    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Missing required template parameters for '{getattr(template, 'id', 'template')}': "
                + ", ".join(missing)
            ),
        )

    _merge_extra_template_params(resolved, supplied)
    return resolved


def _persist_resolved_template_params(service: Service, template_id: str, template_parameters: dict) -> None:
    cfg = dict(service.config or {}) if isinstance(getattr(service, "config", None), dict) else {}
    cfg["template_id"] = template_id
    cfg["template_parameters"] = template_parameters
    service.config = cfg
    flag_modified(service, "config")


def _queue_template_install_for_service(
    db: Session,
    service: Service,
    template_id: str,
    template_parameters: dict | None,
) -> tuple[BootTask, object]:
    """
    Queue a template-based OS installation for the given service's linked server.

    This mirrors the template-handling logic in the server interaction API
    but is scoped for billing usage.
    """
    server = service_linked_server(db, service)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Service has no associated server",
        )

    # Prevent parallel installs on the same server
    active_task = InstallationTaskDAO.get_active_by_server(db, server.id)
    if active_task:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An OS installation is already in progress for this server",
        )

    # Resolve PXE base URL using DHCP config (same logic as PXE endpoint)
    pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server.id)
    base_url = _get_base_url_for_pxe_ip(db, pxe_port.pxe_ip if pxe_port else None)

    template_service = get_template_service()
    template = template_service.get_template(template_id)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"OS template '{template_id}' not found",
        )

    template_parameters = resolve_template_parameters(template, template_parameters)
    _persist_resolved_template_params(service, template_id, template_parameters)

    # Load template script
    script_path = template_service.get_template_script_path(template_id)
    if not script_path or not script_path.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Template '{template_id}' missing installation script",
        )

    with open(script_path, "r") as f:
        script_content = f.read()

    # Build base replacements (server + disk info)
    pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server.id)
    server_mac = pxe_port.mac_address if pxe_port else None
    os_disk = DiskDAO.get_os_disk(db, server.id)

    replacements: dict[str, str] = {
        "SERVER_IP": server.server_ip or "",
        "SERVER_MAC": server_mac or "",
        "SERVER_ID": str(server.id),
        "OS_BOOT_MODE": server.os_boot_mode.value if server.os_boot_mode else "uefi",
    }

    if os_disk:
        replacements["OS_DISK_SERIAL"] = os_disk.serial_number or ""
        replacements["OS_DISK_SIZE_GB"] = str(os_disk.capacity_gb)
        replacements["OS_DISK_TYPE"] = (
            os_disk.type.value.lower()
            if hasattr(os_disk.type, "value")
            else str(os_disk.type).lower()
        )
    else:
        replacements["OS_DISK_SERIAL"] = ""
        replacements["OS_DISK_SIZE_GB"] = ""
        replacements["OS_DISK_TYPE"] = ""

    # Template parameters (e.g. admin_password). These are customer-supplied
    # (set at WHMCS checkout / change-password) and are embedded via literal
    # text substitution into a shell script executed as root on the target
    # server, so they must be escaped for the double-quoted shell-string
    # context templates use (e.g. ADMIN_PASSWORD="${PARAM_ADMIN_PASSWORD}")
    # to prevent shell/command injection.
    template_parameters = template_parameters or {}
    for param_name, param_value in template_parameters.items():
        replacements[f"PARAM_{param_name.upper()}"] = shell_escape_double_quoted(param_value)

    # Use debian-live temp OS for installation (same as other template flows)
    temp_os_service = get_temp_os_service()
    kernel_url = temp_os_service.get_kernel_url("debian-live", base_url)
    initrd_url = temp_os_service.get_initrd_url("debian-live", base_url)
    kernel_params = temp_os_service.get_kernel_params("debian-live")

    # Add squashfs fetch URL
    squashfs_url = temp_os_service.get_squashfs_url("debian-live", base_url)
    if squashfs_url and "fetch=" not in kernel_params:
        kernel_params = f"{kernel_params} fetch={squashfs_url}"

    # Determine which files the script may need (new deploy/ layout or legacy disk_image)
    template_image_files: list[str] = []
    allowed_files: list[str] = []

    deploy_dir = template.template_dir / "deploy" if template.template_dir else None
    if deploy_dir and deploy_dir.exists() and deploy_dir.is_dir():
        # Images live under deploy/, we refer to them via relative paths
        # like "deploy/windows.img" so template-files can serve them from
        # the template root directory.
        for img_file in ["windows.img", "efi.img"]:
            img_path = deploy_dir / img_file
            if img_path.exists():
                rel_name = f"deploy/{img_file}"
                template_image_files.append(rel_name)

    if template.disk_image and not template_image_files:
        disk_image_filename = template.disk_image.split("/")[-1]
        allowed_files.append(disk_image_filename)

    # Scope the token to THIS template's own files, bound to this specific
    # template_id (never a global "*"), so it can't be replayed to read
    # arbitrary scripts/templates/ISOs elsewhere in the system, or against a
    # same-named file in a different template.
    if template.template_dir:
        for rel_path in template_service.enumerate_relative_files(template_id):
            allowed_files.append(template_file_scope(template_id, rel_path))

    # Create boot task
    boot_task = BootTaskDAO.create(
        db=db,
        server_id=server.id,
        boot_type=BootType.TEMP_OS,
        kernel_url=kernel_url,
        initrd_url=initrd_url,
        kernel_params=kernel_params,
        script_url=None,
        script_content=script_content,
        iso_url=None,
        temp_os_id="debian-live",
        description=f"Install OS template '{template.name}' via billing API",
    )

    # Generate download token scoped to this template's own files plus log
    # uploads for this installation task (never a global "*").
    download_token_service = get_download_token_service()
    download_token = download_token_service.generate_token(
        boot_task_id=boot_task.id,
        allowed_files=allowed_files if allowed_files else None,
        allowed_patterns=["logs-*"],
        expires_in=3600,
    )

    # Inject token-based URLs and all variables into script
    if download_token:
        replacements["DOWNLOAD_TOKEN"] = download_token
        replacements["API_BASE_URL"] = base_url
        replacements["TEMPLATE_ID"] = template_id

        if template_image_files:
            for img_file in template_image_files:
                if img_file.endswith("windows.img"):
                    windows_url = (
                        f"{base_url}/api/servers/interaction/template-files/"
                        f"{template_id}/{img_file}?token={download_token}"
                    )
                    replacements["WINDOWS_IMG_URL"] = windows_url
                elif img_file.endswith("efi.img"):
                    efi_url = (
                        f"{base_url}/api/servers/interaction/template-files/"
                        f"{template_id}/{img_file}?token={download_token}"
                    )
                    replacements["EFI_IMG_URL"] = efi_url
        elif template.disk_image:
            disk_image_filename = template.disk_image.split("/")[-1]
            disk_image_url = (
                f"{base_url}/api/servers/interaction/disk-images/"
                f"{disk_image_filename}?token={download_token}"
            )
            replacements["DISK_IMAGE_URL"] = disk_image_url

    # Perform variable substitution in script (including ${VAR:-default} bash syntax)
    for var_name, var_value in replacements.items():
        script_content = script_content.replace(f"${{{var_name}}}", var_value)
        script_content = re.sub(
            r"\$\{" + re.escape(var_name) + r":-[^}]*\}",
            var_value,
            script_content,
        )
        script_content = script_content.replace(f"${var_name}", var_value)

    # Persist updated script to boot task
    boot_task.script_content = script_content
    db.commit()
    db.refresh(boot_task)

    # Tokenized script_url for debian-live (PXE will mint a fresh token at serve time too)
    if boot_task.temp_os_id == "debian-live" and boot_task.script_content:
        boot_task.kernel_params = _inject_script_url_param(
            boot_task.kernel_params, base_url, boot_task.id
        )
        db.commit()
        db.refresh(boot_task)

    # Create installation task for tracking
    installation_task = InstallationTaskDAO.create(
        db=db,
        server_id=server.id,
        boot_task_id=boot_task.id,
        template_id=template_id,
        template_parameters=template_parameters or {},
        os_name=template.name,
    )

    # Add INSTALLATION_TASK_ID into script so installer can report logs
    replacements["INSTALLATION_TASK_ID"] = str(installation_task.id)
    updated_script = boot_task.script_content
    for var_name, var_value in replacements.items():
        updated_script = updated_script.replace(f"${{{var_name}}}", var_value)
        updated_script = re.sub(
            r"\$\{" + re.escape(var_name) + r":-[^}]*\}",
            var_value,
            updated_script,
        )
        updated_script = updated_script.replace(f"${var_name}", var_value)

    boot_task.script_content = updated_script
    db.commit()
    db.refresh(boot_task)

    # Persist credentials on the server object for later retrieval (e.g. billing API)
    if template_parameters:
        credentials = server.credentials or {}
        credentials.update(
            {
                "os_type": template.os_type,
                "template_id": template_id,
                "last_updated": boot_task.created_at.isoformat()
                if boot_task.created_at
                else None,
            }
        )
        for param_name, param_value in template_parameters.items():
            credentials[param_name] = param_value
        server.credentials = credentials
        db.commit()

    logger.info(
        "Queued OS installation task %s for service %s on server %s "
        "(template_id=%s)",
        installation_task.id,
        service.id,
        server.id,
        template_id,
    )

    return boot_task, installation_task


def _provision_bare_metal_service(
    service_data: BillingBareMetalServiceCreate,
    owner_user_id: int,
    actor: ProvisioningActor,
    db: Session,
):
    """Create a bare-metal or http_proxy service via ProvisioningService."""
    try:
        req = from_billing_bare_metal(service_data, owner_user_id)
        return ProvisioningService.create(db, req, actor)
    except ProvisioningError as exc:
        _raise_provisioning(exc)


def provision_bare_metal_service(
    *,
    db: Session,
    service_data: BillingBareMetalServiceCreate,
    owner_user_id: int,
    actor: ProvisioningActor,
):
    return _provision_bare_metal_service(service_data, owner_user_id, actor, db)


@router.post(
    "/bare-metal/services",
    response_model=BillingServiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_bare_metal_service(
    service_data: BillingBareMetalServiceCreate,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    billing_user = ensure_user_for_billing_identity(
        db,
        billing_integration_id=integration.id,
        external_user_id=service_data.external_user_id,
        external_username=service_data.external_username,
        external_email=service_data.external_email,
    )
    service = provision_bare_metal_service(
        db=db,
        service_data=service_data,
        owner_user_id=billing_user.id,
        actor=ProvisioningActor(
            kind="integration",
            actor_id=integration.id,
            name=integration.name,
            source="billing_api",
        ),
    )
    return _billing_service_response(db, service)


def _billing_vm_product_snapshot(
    db: Session,
    product_code: Optional[str],
    *,
    vm_template_id: Optional[int] = None,
) -> tuple[dict, Optional[str]]:
    if not product_code:
        return {}, None
    try:
        return build_product_snapshot(
            db,
            product_code,
            None,
            ServiceType.VM,
            vm_template_id=vm_template_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _provision_vm_service(
    body: BillingVmServiceCreate,
    owner_user_id: int,
    actor: ProvisioningActor,
    db: Session,
):
    """Create a VM service via ProvisioningService."""
    try:
        req = from_billing_vm(body, owner_user_id)
        return ProvisioningService.create(db, req, actor)
    except ProvisioningError as exc:
        _raise_provisioning(exc)


def provision_vm_service(
    *,
    db: Session,
    body: BillingVmServiceCreate,
    owner_user_id: int,
    actor: ProvisioningActor,
):
    return _provision_vm_service(body, owner_user_id, actor, db)


@router.post(
    "/vm/services",
    response_model=BillingServiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_vm_service(
    body: BillingVmServiceCreate,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    billing_user = ensure_user_for_billing_identity(
        db,
        billing_integration_id=integration.id,
        external_user_id=body.external_user_id,
        external_username=body.external_username,
        external_email=body.external_email,
    )
    service = provision_vm_service(
        db=db,
        body=body,
        owner_user_id=billing_user.id,
        actor=ProvisioningActor(
            kind="integration",
            actor_id=integration.id,
            name=integration.name,
            source="billing_api",
        ),
    )
    return _billing_service_response(db, service)


@router.get("/services", response_model=List[BillingServiceResponse], responses=COMMON_ERROR_RESPONSES)
async def list_services(
    skip: int = 0,
    limit: int = 100,
    status_filter: Optional[str] = None,
    *,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    List services accessible via billing API.
    
    Only returns services owned by users belonging to this integration.
    """
    # Get all users with a billing identity under this integration
    billing_users = UserDAO.get_by_billing_integration(db, integration.id)
    owner_user_ids = [u.id for u in billing_users]
    
    if not owner_user_ids:
        return []
    
    # Filter services by owner user IDs
    query = db.query(Service).filter(Service.owner_user_id.in_(owner_user_ids))
    
    # Apply status filter if provided
    if status_filter:
        try:
            status_enum = ServiceStatus(status_filter.lower())
            query = query.filter(Service.status == status_enum)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status: {status_filter}. Must be 'active', 'suspended', 'terminated', or 'pending'"
            )
    
    services = query.order_by(Service.name).offset(skip).limit(limit).all()
    
    return [_billing_service_response(db, s) for s in services]


def _lookup_services_integration_query(db: Session, integration: BillingIntegration):
    owner = aliased(User)
    return (
        db.query(Service)
        .outerjoin(owner, Service.owner_user_id == owner.id)
        .outerjoin(ServiceVm, ServiceVm.service_id == Service.id)
        .outerjoin(ServiceBareMetal, ServiceBareMetal.service_id == Service.id)
        .outerjoin(Server, Server.id == ServiceBareMetal.server_id)
        .filter(
            or_(
                Service.owner_user_id.is_(None),
                owner.billing_integration_id.is_(None),
                owner.billing_integration_id == integration.id,
            )
        )
    )


def _apply_lookup_server_ip_filter(
    query,
    db: Session,
    ip: str,
    *,
    text: Optional[str],
    proxmox_vmid: Optional[int],
):
    server = ServerDAO.get_by_ip(db, ip)
    if not server:
        if text is None and proxmox_vmid is None:
            return query, True
        return query.filter(Service.id == -1), False
    return query.filter(ServiceBareMetal.server_id == server.id), False


def _apply_lookup_text_filter(query, text: str):
    like = f"%{text}%"
    vmid_prefix = f"{text}%"
    clauses = [
        Service.name.ilike(like),
        cast(ServiceVm.proxmox_vmid, String).like(vmid_prefix),
        cast(ServiceVm.proxmox_vmid, String).like(like),
        Server.server_ip.ilike(like),
        Server.name.ilike(like),
    ]
    if text.isdigit():
        clauses.append(Service.id == int(text))
        clauses.append(ServiceVm.proxmox_vmid == int(text))
    return query.filter(or_(*clauses))


async def _append_proxmox_lookup_results(
    db: Session,
    results: List[BillingServiceLookupItem],
    *,
    text: Optional[str],
    proxmox_vmid: Optional[int],
) -> None:
    proxmox_q = text
    if proxmox_q is None and proxmox_vmid is not None:
        proxmox_q = str(int(proxmox_vmid))
    if not proxmox_q:
        return

    from app.services.proxmox_vm_search import search_proxmox_vms

    seen_vmids = {
        (r.proxmox_cluster_id, r.proxmox_vmid)
        for r in results
        if r.proxmox_cluster_id is not None and r.proxmox_vmid is not None
    }
    for guest in await search_proxmox_vms(db, proxmox_q, limit=25):
        key = (guest["cluster_id"], guest["vmid"])
        if key in seen_vmids:
            continue
        seen_vmids.add(key)
        results.append(
            BillingServiceLookupItem(
                id=None,
                name=guest["name"],
                service_type="vm",
                status=guest.get("status") or None,
                proxmox_cluster_id=guest["cluster_id"],
                proxmox_node_name=guest["node_name"],
                proxmox_vmid=guest["vmid"],
                source="proxmox",
                proxmox_status=guest.get("status") or None,
            )
        )


@router.get("/services/lookup", response_model=List[BillingServiceLookupItem], responses=COMMON_ERROR_RESPONSES)
async def lookup_services(
    q: Optional[str] = None,
    service_id: Optional[int] = None,
    proxmox_vmid: Optional[int] = None,
    server_ip: Optional[str] = None,
    *,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """
    Look up services for WHMCS admin linking.

    Accepts exact filters (``service_id``, ``proxmox_vmid``, ``server_ip``) and/or
    a free-text ``q`` that matches service id, service name, VMID (prefix/substring),
    server IP, or server name. Unowned/internal services are included so they can
    be claimed. When ``q`` or ``proxmox_vmid`` is set, also searches live Proxmox
    guests and returns unmanaged matches with ``source=proxmox``.
    """
    ip = (server_ip or "").strip() or None
    text = (q or "").strip() or None
    if service_id is None and proxmox_vmid is None and ip is None and text is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide q, service_id, proxmox_vmid, and/or server_ip",
        )

    query = _lookup_services_integration_query(db, integration)
    if service_id is not None:
        query = query.filter(Service.id == int(service_id))
    if proxmox_vmid is not None:
        query = query.filter(ServiceVm.proxmox_vmid == int(proxmox_vmid))
    if ip is not None:
        query, empty = _apply_lookup_server_ip_filter(
            query, db, ip, text=text, proxmox_vmid=proxmox_vmid
        )
        if empty:
            return []

    if text is not None:
        query = _apply_lookup_text_filter(query, text)

    services = query.distinct().order_by(Service.id).limit(50).all()
    results: List[BillingServiceLookupItem] = [
        _lookup_item_from_service(db, s) for s in services
    ]

    await _append_proxmox_lookup_results(
        db, results, text=text, proxmox_vmid=proxmox_vmid
    )

    return results[:50]


def _validate_adopt_vm_inputs(db: Session, cluster_id: int, node_name: str, vmid: int) -> None:
    if cluster_id <= 0 or node_name == "" or vmid <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="proxmox_cluster_id, proxmox_node_name, and proxmox_vmid are required",
        )
    if ProxmoxInventoryDAO.get_cluster(db, cluster_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proxmox cluster not found")
    conflict = (
        db.query(ServiceVm)
        .filter(
            ServiceVm.proxmox_cluster_id == cluster_id,
            ServiceVm.proxmox_vmid == vmid,
        )
        .first()
    )
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"VMID {vmid} is already bound to RackFlow service {conflict.service_id}",
        )


def _assert_adopt_external_service_available(
    db: Session, external_service_id: str, integration: BillingIntegration
) -> None:
    existing = ServiceDAO.get_by_external_service_id_and_integration(
        db, external_service_id, integration.id
    )
    if existing and existing.status != ServiceStatus.TERMINATED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"External service id '{external_service_id}' is already linked to "
                f"RackFlow service {existing.id}"
            ),
        )


def _unique_adopt_service_name(db: Session, body: BillingAdoptVmService, vmid: int) -> str:
    base_name = (body.name or "").strip() or f"vm-{vmid}"
    name = base_name
    suffix = 1
    while ServiceDAO.get_by_name(db, name):
        suffix += 1
        name = f"{base_name}-{suffix}"
    return name


@router.post("/services/adopt-vm", response_model=BillingServiceResponse, status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
async def adopt_vm_service(
    body: BillingAdoptVmService,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """
    Create a billing VM service bound to an existing Proxmox guest (no clone/provision).
    """
    from app.models.service_vm import VMGuestState

    cluster_id = int(body.proxmox_cluster_id)
    node_name = (body.proxmox_node_name or "").strip()
    vmid = int(body.proxmox_vmid)
    _validate_adopt_vm_inputs(db, cluster_id, node_name, vmid)

    external_service_id = (body.external_service_id or "").strip()
    if not external_service_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="external_service_id is required",
        )
    _assert_adopt_external_service_available(db, external_service_id, integration)

    billing_user = ensure_user_for_billing_identity(
        db,
        billing_integration_id=integration.id,
        external_user_id=body.external_user_id,
        external_username=body.external_username,
        external_email=body.external_email,
    )

    name = _unique_adopt_service_name(db, body, vmid)
    product_snapshot, effective_os_code = _billing_vm_product_snapshot(
        db, body.product_code
    )

    service = ServiceDAO.create_vm(
        db,
        name=name,
        owner_user_id=billing_user.id,
        external_service_id=external_service_id,
        status=ServiceStatus.ACTIVE,
        product_code=body.product_code,
        os_code=effective_os_code,
        product_snapshot=product_snapshot,
        provisioning_source=ProvisioningSource.BILLING,
        proxmox_cluster_id=cluster_id,
        proxmox_node_name=node_name,
        proxmox_vmid=vmid,
    )
    try:
        reserved = await reserve_vmid_aligned_with_proxmox(
            db,
            cluster_id=cluster_id,
            service_id=service.id,
            node_name=node_name,
            requested_vmid=vmid,
            adopt_existing=True,
        )
    except ValueError as exc:
        ServiceDAO.delete(db, service.id)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    service.vm.proxmox_vmid = int(reserved)
    service.vm.guest_state = VMGuestState.STOPPED
    # Best-effort IP pool assign; adopted guests often already have networking.
    VMIPAllocationDAO.assign_next_free_to_service(
        db,
        service_id=service.id,
        proxmox_cluster_id=cluster_id,
    )
    ServiceDAO.update(db, service)
    db.refresh(service)
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="adopt_vm",
        source="billing_api",
        message=f"Adopted existing Proxmox VMID {vmid}",
        details={
            "cluster_id": cluster_id,
            "node_name": node_name,
            "vmid": int(reserved),
            "external_service_id": external_service_id,
        },
    )
    return _billing_service_response(db, service)


@router.get("/services/{service_id}", response_model=BillingServiceDetailResponse, responses=COMMON_ERROR_RESPONSES)
async def get_service(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """Get service details by ID"""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG
        )
    
    _assert_billing_owned_service(service, integration)

    server = service_linked_server(db, service)
    payload = _billing_service_response(db, service).model_dump()
    payload["server"] = (
        {
            "id": server.id,
            "name": server.name,
            "server_ip": server.server_ip,
            "description": server.description,
            "cpu_count": server.cpu_count,
            "cpu_model": server.cpu_model,
            "ram_gb": server.ram_gb,
            "port_speed_mbps": server.port_speed_mbps,
            "enabled": server.enabled,
            "os_boot_mode": server.os_boot_mode.value,
        }
        if server
        else None
    )
    owner = service.owner_user
    payload["external_user"] = (
        {
            "id": owner.id,
            "external_user_id": owner.external_user_id,
            "external_username": owner.external_username,
            "external_email": owner.external_email,
        }
        if owner is not None and owner.billing_integration_id
        else None
    )
    return BillingServiceDetailResponse(**payload)


def _link_service_assign_owner(
    service: Service,
    data: BillingLinkService,
    integration: BillingIntegration,
    db: Session,
) -> None:
    """Claim an unowned service or reassign billing ownership when external_user_id is set."""
    currently_billed = (
        service.owner_user is not None and service.owner_user.billing_integration_id is not None
    )
    need_user = not currently_billed or (
        data.external_user_id is not None and str(data.external_user_id).strip() != ""
    )
    if not need_user:
        return

    ext_id = (
        str(data.external_user_id).strip()
        if data.external_user_id is not None and str(data.external_user_id).strip() != ""
        else None
    )
    if not ext_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="external_user_id is required when claiming an unowned service",
        )
    billing_user = ensure_user_for_billing_identity(
        db,
        billing_integration_id=integration.id,
        external_user_id=ext_id,
        external_username=data.external_username,
        external_email=data.external_email,
    )
    service.owner_user_id = billing_user.id
    if service.provisioning_source == ProvisioningSource.INTERNAL:
        service.provisioning_source = ProvisioningSource.BILLING


@router.post("/services/{service_id}/link", response_model=BillingServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def link_service(
    service_id: int,
    data: BillingLinkService,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """
    Bind an existing RackFlow service to an external line item (e.g. WHMCS hosting id).

    Sets ``external_service_id``. When ``external_user_id`` is provided, finds or creates
    that external user under this integration and assigns ownership.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_linkable_service(service, integration)

    external_service_id = (data.external_service_id or "").strip()
    if not external_service_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="external_service_id is required",
        )

    existing = ServiceDAO.get_by_external_service_id_and_integration(
        db, external_service_id, integration.id
    )
    if existing and existing.id != service.id and existing.status != ServiceStatus.TERMINATED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"External service id '{external_service_id}' is already linked to "
                f"RackFlow service {existing.id}"
            ),
        )

    _link_service_assign_owner(service, data, integration, db)

    service.external_service_id = external_service_id
    ServiceDAO.update(db, service)
    db.refresh(service)
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="link",
        source="billing_api",
        message=f"Linked external_service_id={external_service_id}",
        details={"external_service_id": external_service_id},
    )
    return _billing_service_response(db, service)


@router.post("/services/{service_id}/unlink", response_model=BillingServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def unlink_service(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """Clear ``external_service_id`` on a billing-owned service (does not terminate)."""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)

    previous = service.external_service_id
    service.external_service_id = None
    ServiceDAO.update(db, service)
    db.refresh(service)
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="unlink",
        source="billing_api",
        message="Cleared external_service_id",
        details={"previous_external_service_id": previous},
    )
    return _billing_service_response(db, service)


@router.put("/services/{service_id}/vm/placement", response_model=BillingServiceResponse, responses=COMMON_ERROR_RESPONSES)
async def update_vm_placement(
    service_id: int,
    body: BillingVmPlacementUpdate,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """Update Proxmox cluster/node/VMID for a VM billing service."""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.service_type != ServiceType.VM or not service.vm:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM service not found")
    _assert_billing_owned_service(service, integration)

    if ProxmoxInventoryDAO.get_cluster(db, body.proxmox_cluster_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proxmox cluster not found")

    node_name = (body.proxmox_node_name or "").strip()
    if not node_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="proxmox_node_name is required",
        )

    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="update_vm_placement",
        source="billing_api",
        message="Updating VM placement",
        details={
            "cluster_id": body.proxmox_cluster_id,
            "node_name": node_name,
            "requested_vmid": body.proxmox_vmid,
        },
    )
    try:
        reserved_vmid = await reserve_vmid_aligned_with_proxmox(
            db,
            cluster_id=body.proxmox_cluster_id,
            service_id=service.id,
            node_name=node_name,
            requested_vmid=body.proxmox_vmid,
            adopt_existing=bool(body.adopt_existing),
        )
    except ValueError as exc:
        log_server_activity_failure(
            db,
            service_id=service.id,
            event_type=ServerActivityEventType.SERVICE,
            action="update_vm_placement",
            source="billing_api",
            message=str(exc),
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    service.vm.proxmox_cluster_id = body.proxmox_cluster_id
    service.vm.proxmox_node_name = node_name
    service.vm.proxmox_vmid = int(reserved_vmid)
    if body.adopt_existing and body.proxmox_vmid is not None:
        from app.models.service_vm import VMGuestState

        service.vm.guest_state = VMGuestState.STOPPED
        service.status = ServiceStatus.ACTIVE
    ServiceDAO.update(db, service)
    db.refresh(service)
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="update_vm_placement",
        source="billing_api",
        message="Updated VM placement",
        details={
            "cluster_id": body.proxmox_cluster_id,
            "node_name": node_name,
            "vmid": int(reserved_vmid),
            "adopt_existing": bool(body.adopt_existing),
        },
    )
    return _billing_service_response(db, service)


@router.delete("/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
async def terminate_service(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """
    Terminate a service.

    This marks the service as terminated.
    The server and service records are not deleted (for audit purposes).
    Server enabled/disabled is an administrative server-level flag and is not
    changed by service lifecycle actions.
    """
    logger.info(f"Billing API: Terminating service {service_id} via integration '{integration.name}'")

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG
        )

    _assert_billing_owned_service(service, integration)

    VMIPAllocationDAO.release_for_service(db, service.id)
    IPAMDAO.release_all_for_service(db, service.id, released_by=f"billing:{integration.name}")

    if service.service_type == ServiceType.VM:
        try:
            from app.services.vm_backup_service import purge_client_backups

            await purge_client_backups(db, service)
        except Exception:
            logger.exception(
                "Billing API: failed to purge client backups for service %s", service_id
            )

    log_kw = _billing_activity_log_kw(db, service)
    log_server_activity_attempt(
        db,
        **log_kw,
        event_type=ServerActivityEventType.SERVICE,
        action="terminate",
        source="billing_api",
        message=f"Terminating service {service.id}",
        details={"service_id": service.id, "integration_id": integration.id},
    )
    service.status = ServiceStatus.TERMINATED
    service.terminated_at = datetime.now(timezone.utc)
    ServiceDAO.update(db, service)

    log_server_activity_success(
        db,
        **log_kw,
        event_type=ServerActivityEventType.SERVICE,
        action="terminate",
        source="billing_api",
        message=f"Terminated service {service.id}",
        details={"service_id": service.id, "integration_id": integration.id},
    )
    
    logger.info(f"Billing API: Service {service_id} terminated successfully")
    return None


@router.post("/services/{service_id}/suspend", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def suspend_service(
    service_id: int,
    action: SuspendAction,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    Suspend a service.

    Ensures the linked machine is force-powered off (when applicable), then
    marks the service suspended. Suspended services are blocked from power-on
    actions via service status. Server enabled/disabled is an administrative
    server-level flag and is not changed by service lifecycle actions.
    """
    logger.info(f"Billing API: Suspending service {service_id} via integration '{integration.name}'")
    
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG
        )
    
    _assert_billing_owned_service(service, integration)

    log_kw = _billing_activity_log_kw(db, service)
    log_server_activity_attempt(
        db,
        **log_kw,
        event_type=ServerActivityEventType.SERVICE,
        action="suspend",
        source="billing_api",
        message=f"Suspending service {service.id}",
        details={
            "service_id": service.id,
            "integration_id": integration.id,
            "reason": action.reason,
        },
    )
    try:
        await _ensure_service_powered_off(db, service)
    except HTTPException as exc:
        log_server_activity_failure(
            db,
            **log_kw,
            event_type=ServerActivityEventType.SERVICE,
            action="suspend",
            source="billing_api",
            message=f"Suspend failed for service {service.id}",
            details={
                "service_id": service.id,
                "integration_id": integration.id,
                "reason": action.reason,
                "detail": exc.detail,
            },
            error=exc,
        )
        raise

    service.status = ServiceStatus.SUSPENDED
    ServiceDAO.update(db, service)

    log_server_activity_success(
        db,
        **log_kw,
        event_type=ServerActivityEventType.SERVICE,
        action="suspend",
        source="billing_api",
        message=f"Suspended service {service.id}",
        details={
            "service_id": service.id,
            "integration_id": integration.id,
            "reason": action.reason,
        },
    )
    
    logger.info(f"Billing API: Service {service_id} suspended (reason: {action.reason})")
    return {"status": "suspended", "message": "Service has been suspended"}


@router.post("/services/{service_id}/unsuspend", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def unsuspend_service(
    service_id: int,
    action: SuspendAction,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    Unsuspend a service.
    
    Re-enables a suspended service.
    Server enabled/disabled is an administrative server-level flag and is not
    changed by service lifecycle actions.
    """
    logger.info(f"Billing API: Unsuspending service {service_id} via integration '{integration.name}'")
    
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG
        )
    
    _assert_billing_owned_service(service, integration)

    log_kw = _billing_activity_log_kw(db, service)
    log_server_activity_attempt(
        db,
        **log_kw,
        event_type=ServerActivityEventType.SERVICE,
        action="unsuspend",
        source="billing_api",
        message=f"Unsuspending service {service.id}",
        details={"service_id": service.id, "integration_id": integration.id},
    )
    await ServiceLifecycle().unsuspend(
        db, service, reason=action.reason or "billing_api"
    )
    db.commit()

    log_server_activity_success(
        db,
        **log_kw,
        event_type=ServerActivityEventType.SERVICE,
        action="unsuspend",
        source="billing_api",
        message=f"Unsuspended service {service.id}",
        details={"service_id": service.id, "integration_id": integration.id},
    )
    
    logger.info(f"Billing API: Service {service_id} unsuspended")
    return {"status": "active", "message": "Service has been unsuspended"}


@router.post("/services/{service_id}/power", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def power_control(
    service_id: int,
    power_action: PowerAction,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    Control server power state via service.

    Actions: on, off, reboot, reset.

    Authenticated via billing API key (operator authority). Client portal
    power gating uses client-facing routes and permission resolution separately;
    this endpoint is not gated by VM_POWER / BMS_POWER.
    """
    logger.info(f"Billing API: Power action '{power_action.action}' on service {service_id} via integration '{integration.name}'")
    
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG
        )
    
    _assert_billing_owned_service(service, integration)

    log_kw = _billing_activity_log_kw(db, service)
    server = service_linked_server(db, service)

    action = power_action.action.lower()
    log_server_activity_attempt(
        db,
        **log_kw,
        event_type=ServerActivityEventType.POWER,
        action=action,
        source="billing_api",
        message=f"Power action '{action}' requested",
        details={"service_id": service.id, "integration_id": integration.id},
    )

    try:
        # Any action that can leave the machine running (on/reboot/reset) is
        # forbidden for suspended/terminated services; only "off" is allowed so
        # a suspended box can still be powered down.
        if action in ("on", "reboot", "reset"):
            if service.status in (ServiceStatus.SUSPENDED, ServiceStatus.TERMINATED):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Cannot '{action}' server for a {service.status.value} service",
                )
            if server is not None and not server.enabled:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Cannot '{action}' an administratively disabled server",
                )

        plugin_instance, _srv = await _billing_get_plugin_instance(db, service)
        if not plugin_instance:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to initialize plugin",
            )

        success = False
        if action == "on":
            success = await plugin_instance.power_on()
        elif action == "off":
            success = await plugin_instance.power_off(force=False)
        elif action == "reboot":
            success = await plugin_instance.power_reset()
        elif action == "reset":
            success = await plugin_instance.power_reset()
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid power action: {action}. Must be 'on', 'off', 'reboot', or 'reset'"
            )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Power action command failed",
            )
    except NotImplementedError as exc:
        log_server_activity_failure(
            db,
            **log_kw,
            event_type=ServerActivityEventType.POWER,
            action=action,
            source="billing_api",
            message=f"Power action '{action}' failed",
            details={"service_id": service.id, "integration_id": integration.id},
            error=exc,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server plugin does not support power control"
        )
    except HTTPException as exc:
        log_server_activity_failure(
            db,
            **log_kw,
            event_type=ServerActivityEventType.POWER,
            action=action,
            source="billing_api",
            message=f"Power action '{action}' failed",
            details={
                "service_id": service.id,
                "integration_id": integration.id,
                "detail": exc.detail,
            },
        )
        raise
    except Exception as e:
        logger.error(f"Billing API: Power action failed: {e}", exc_info=True)
        log_server_activity_failure(
            db,
            **log_kw,
            event_type=ServerActivityEventType.POWER,
            action=action,
            source="billing_api",
            message=f"Power action '{action}' failed",
            details={"service_id": service.id, "integration_id": integration.id},
            error=e,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Power action failed: {str(e)}"
        )

    log_server_activity_success(
        db,
        **log_kw,
        event_type=ServerActivityEventType.POWER,
        action=action,
        source="billing_api",
        message=f"Power action '{action}' completed",
        details={"service_id": service.id, "integration_id": integration.id},
    )
    logger.info(f"Billing API: Power action '{action}' on service {service_id} completed successfully")
    return {"status": "success", "action": action, "message": f"Server power {action} command executed"}


@router.get("/services/{service_id}/status", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def get_service_status(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    Get service status including server power state.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG
        )
    
    _assert_billing_owned_service(service, integration)

    server = service_linked_server(db, service)

    power_state = PowerState.UNKNOWN
    try:
        plugin_instance, _ = await _billing_get_plugin_instance(db, service)
        if plugin_instance:
            power_state = await plugin_instance.get_power_state()
    except HTTPException:
        power_state = PowerState.UNKNOWN
    except Exception as e:
        logger.warning(f"Billing API: Could not get power state for service {service_id}: {e}")

    installation = None
    if server:
        active = InstallationTaskDAO.get_active_by_server(db, server.id)
        hist = InstallationTaskDAO.get_by_server(db, server.id)
        task = active or (hist[0] if hist else None)
        if task:
            installation = {
                "task_id": task.id,
                "status": task.status.value,
                "progress_percent": task.progress_percent,
                "os_name": task.os_name,
                "error_message": task.error_message,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            }

    client_permissions = resolve_client_permissions(db, service)
    power_key = _power_permission_key(service)
    ipmi_granted = bool(client_permissions.get(PermissionKey.BMS_IPMI, False))
    kvm_granted = bool(client_permissions.get(PermissionKey.BMS_KVM, False))
    sol_granted = bool(client_permissions.get(PermissionKey.BMS_SOL, False))

    ipmi_proxy_available = bool(
        server
        and getattr(server, "ipmi_proxy_enabled", False)
        and getattr(server, "ipmi_web_management_url", None)
        and ipmi_granted
    )
    kvm_console_available = bool(server and kvm_ready(server) and kvm_granted)
    sol_console_available = bool(server and sol_ready(server) and sol_granted)
    virtual_media_granted = bool(client_permissions.get(PermissionKey.BMS_VIRTUAL_MEDIA, False))
    virtual_media_available = bool(server and virtual_media_ready(server) and virtual_media_granted)

    vnc_console_granted = bool(client_permissions.get(PermissionKey.VM_CONSOLE, False))
    vnc_cid, _vnc_node, vnc_vmid = vm_placement(service)
    vnc_console_available = bool(
        service.service_type == ServiceType.VM
        and vnc_cid is not None
        and vnc_vmid is not None
        and vnc_console_granted
    )
    backups_available = bool(
        service.service_type == ServiceType.VM
        and vnc_cid is not None
        and vnc_vmid is not None
        and client_permissions.get(PermissionKey.VM_BACKUPS, False)
    )

    proxy_credentials_available = bool(
        service.service_type == ServiceType.HTTP_PROXY
        and client_permissions.get(PermissionKey.PROXY_VIEW_CREDENTIALS, False)
    )
    proxy_rotate_available = bool(
        service.service_type == ServiceType.HTTP_PROXY
        and client_permissions.get(PermissionKey.PROXY_ROTATE_CREDENTIALS, False)
    )
    proxy_assignments = (
        [assignment_payload(a) for a in IPAMDAO.get_assignment_by_service(db, service.id)]
        if proxy_credentials_available
        else None
    )

    cid, node, vmid = vm_placement(service)
    vm_ip_address = None
    vm_ip_allocation_id = None
    if service.service_type == ServiceType.VM and service.vm:
        vm_ip_allocation_id = service.vm.vm_ip_allocation_id
        if service.vm.vm_ip_allocation:
            vm_ip_address = service.vm.vm_ip_allocation.ip_address
        if not vm_ip_address:
            vm_ip_address = (service.config or {}).get("vm_ip_address")
        if not vm_ip_allocation_id:
            vm_ip_allocation_id = (service.config or {}).get("vm_ip_allocation_id")
    return {
        "service_id": service.id,
        "service_name": service.name,
        "service_status": service.status.value,
        "service_type": service.service_type.value if service.service_type else None,
        "server_id": server.id if server else None,
        "server_name": server.name if server else None,
        "server_enabled": server.enabled if server else None,
        "power_state": power_state.value,
        "proxmox_cluster_id": cid,
        "proxmox_node_name": node,
        "proxmox_vmid": vmid,
        "vm_ip_address": vm_ip_address,
        "vm_ip_allocation_id": vm_ip_allocation_id,
        "ipmi_proxy_available": ipmi_proxy_available,
        # Viewer BMC credentials (safe to show to the service owner / WHMCS).
        "ipmi_viewer_username": (
            getattr(server, "ipmi_viewer_username", None) if ipmi_proxy_available else None
        ),
        "ipmi_viewer_password": (
            getattr(server, "ipmi_viewer_password", None) if ipmi_proxy_available else None
        ),
        "kvm_console_available": kvm_console_available,
        "sol_console_available": sol_console_available,
        "virtual_media_available": virtual_media_available,
        "vnc_console_available": vnc_console_available,
        "backups_available": backups_available,
        "proxy_credentials_available": proxy_credentials_available,
        "proxy_rotate_available": proxy_rotate_available,
        "proxy_assignments": proxy_assignments,
        "status": "suspended"
        if service.status == ServiceStatus.SUSPENDED
        else (
            "on"
            if power_state == PowerState.ON
            else "off" if power_state == PowerState.OFF else "unknown"
        ),
        "installation": installation,
        # Effective client permissions (see app.core.client_permissions) so
        # billing integrations (e.g. WHMCS) can gate their own UI without
        # duplicating the resolution hierarchy. power_available mirrors the
        # power permission for this service's type for convenience.
        "client_permissions": client_permissions,
        # VM power always goes through Proxmox placement (no linked Server
        # row by design); bare_metal/http_proxy power requires an actual
        # linked server, which a server-less proxy service may not have.
        "power_available": bool(
            client_permissions.get(power_key, False)
            and (service.service_type == ServiceType.VM or server is not None)
        ),
        # Strategy actions for WHMCS admin/client button arrays
        "admin_actions": list_actions(db, service, "admin"),
        "client_actions": list_actions(db, service, "client"),
    }


@router.post("/services/{service_id}/proxy/rotate", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def rotate_proxy_credentials(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """Rotate credentials for every IP assigned to an http_proxy service.

    Gated by the ``proxy.rotate_credentials`` client permission so WHMCS
    can offer this from the client area only when the product/preset grants
    it; operator/admin use goes through the admin IPAM API instead.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    if service.service_type != ServiceType.HTTP_PROXY:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a proxy service")
    require_client_permission(db, service, PermissionKey.PROXY_ROTATE_CREDENTIALS)

    assignments = IPAMDAO.get_assignment_by_service(db, service.id)
    if not assignments:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Service has no assigned IPs to rotate")

    rotated = []
    for assignment in assignments:
        updated = IPAMDAO.rotate_credentials(
            db,
            assignment_id=assignment.id,
            username=generate_proxy_username(),
            password=generate_proxy_password(),
            rotated_by=f"billing:{integration.name}",
        )
        if updated:
            rotated.append(assignment_payload(updated))

    logger.info("Billing API: rotated proxy credentials for service %s (%s IP(s))", service_id, len(rotated))
    return {"status": "ok", "proxy_assignments": rotated}


class StrategyActionRequest(BaseModel):
    params: dict = Field(default_factory=dict)


class BillingReassignVmIpBody(BaseModel):
    allocation_id: int = Field(..., description="Free VM IP pool row id to assign")
    reset_network: bool = Field(
        True,
        description=(
            "After swapping the pool row, run strategy reset_network. "
            "For Linux cloud-init guests this regenerates cloud-init and reboots the VM."
        ),
    )


@router.get("/services/{service_id}/available-ips", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def billing_list_available_vm_ips(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """Admin: browse free VM IP pool rows for this service's Proxmox cluster."""
    from app.services.vm_ip_reassign import VmIpReassignError, list_available_ips_for_service

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    try:
        return list_available_ips_for_service(db, service)
    except VmIpReassignError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/services/{service_id}/reassign-ip", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def billing_reassign_vm_ip(
    service_id: int,
    body: BillingReassignVmIpBody,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """Admin: release current VM IP, claim a free pool row, optionally reset guest networking."""
    from app.services.vm_ip_reassign import VmIpReassignError, reassign_vm_ip

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    try:
        return await reassign_vm_ip(
            db,
            service,
            allocation_id=body.allocation_id,
            reset_network=body.reset_network,
            source="billing_api",
        )
    except VmIpReassignError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/services/{service_id}/actions", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def billing_list_strategy_actions(
    service_id: int,
    audience: str = "client",
    *,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    if audience not in ("admin", "client"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_AUDIENCE_ADMIN_OR_CLIENT_MSG)
    return {"actions": list_actions(db, service, audience)}  # type: ignore[arg-type]


class BillingBackupCreate(BaseModel):
    notes: Optional[str] = None
    mode: str = "snapshot"
    wait: bool = False


class BillingBackupMutate(BaseModel):
    volid: str
    storage: Optional[str] = None
    wait: bool = False
    start: bool = True
    vm_template_id: Optional[int] = None


def _billing_backup_service(
    db: Session,
    service_id: int,
    integration: BillingIntegration,
    *,
    audience: str,
):
    from app.api.vm_backup_routes import map_backup_error

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_NOT_VM_SERVICE_MSG)
    if audience == "client":
        require_client_permission(db, service, PermissionKey.VM_BACKUPS)
    return service, map_backup_error


@router.get("/services/{service_id}/backups", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def billing_list_backups(
    service_id: int,
    audience: str = "client",
    *,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    from app.services.vm_backup_service import list_service_backups_and_jobs

    if audience not in ("admin", "client"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_AUDIENCE_ADMIN_OR_CLIENT_MSG)
    service, map_err = _billing_backup_service(db, service_id, integration, audience=audience)
    try:
        items, jobs = await list_service_backups_and_jobs(db, service)
    except Exception as exc:
        raise map_err(exc) from exc
    return {"backups": items, "jobs": jobs}


@router.get("/services/{service_id}/backup-jobs", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def billing_list_backup_jobs(
    service_id: int,
    audience: str = "client",
    *,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    from app.services.vm_backup_service import list_running_backup_jobs

    if audience not in ("admin", "client"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_AUDIENCE_ADMIN_OR_CLIENT_MSG)
    service, map_err = _billing_backup_service(db, service_id, integration, audience=audience)
    try:
        jobs = await list_running_backup_jobs(db, service)
    except Exception as exc:
        raise map_err(exc) from exc
    return {"jobs": jobs}


@router.post("/services/{service_id}/backups", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def billing_create_backup(
    service_id: int,
    body: BillingBackupCreate,
    audience: str = "client",
    *,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    from app.services.vm_backup_service import create_client_backup

    if audience not in ("admin", "client"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_AUDIENCE_ADMIN_OR_CLIENT_MSG)
    service, map_err = _billing_backup_service(db, service_id, integration, audience=audience)
    try:
        return await create_client_backup(
            db, service, notes=body.notes, mode=body.mode, wait=body.wait
        )
    except Exception as exc:
        raise map_err(exc) from exc


@router.post("/services/{service_id}/backups/delete", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def billing_delete_backup(
    service_id: int,
    body: BillingBackupMutate,
    audience: str = "client",
    *,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    from app.services.vm_backup_service import delete_client_backup

    if audience not in ("admin", "client"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_AUDIENCE_ADMIN_OR_CLIENT_MSG)
    service, map_err = _billing_backup_service(db, service_id, integration, audience=audience)
    try:
        await delete_client_backup(db, service, volid=body.volid, storage=body.storage)
    except Exception as exc:
        raise map_err(exc) from exc
    return {"status": "ok"}


@router.post("/services/{service_id}/backups/restore", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def billing_restore_backup(
    service_id: int,
    body: BillingBackupMutate,
    audience: str = "client",
    *,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    from app.models.service_vm import VMGuestState
    from app.services.vm_backup_service import restore_service_backup

    if audience not in ("admin", "client"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_AUDIENCE_ADMIN_OR_CLIENT_MSG)
    service, map_err = _billing_backup_service(db, service_id, integration, audience=audience)
    try:
        result = await restore_service_backup(
            db,
            service,
            volid=body.volid,
            storage=body.storage,
            wait=body.wait,
            start=body.start,
            vm_template_id=body.vm_template_id,
        )
    except Exception as exc:
        raise map_err(exc) from exc
    # Async enqueue already sets PROVISIONING; only sync wait path finalizes power state here.
    if body.wait and service.vm:
        service.vm.guest_state = VMGuestState.RUNNING if body.start else VMGuestState.STOPPED
        service.vm.guest_last_error = None
        ServiceDAO.update(db, service)
    return result


class BillingVmReinstallBody(BaseModel):
    vm_template_id: Optional[int] = None
    ssh_public_keys: Optional[str] = None


@router.get("/services/{service_id}/vm/reinstall-options", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def billing_vm_reinstall_options(
    service_id: int,
    audience: str = "admin",
    *,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """Templates available for VM reinstall (product-linked catalog rows)."""
    from app.services.ssh_public_keys import ssh_key_fields_for_service
    from app.services.vm_ssh_keys_service import list_reinstall_templates_for_service

    if audience not in ("admin", "client"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_AUDIENCE_ADMIN_OR_CLIENT_MSG)
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_NOT_VM_SERVICE_MSG)
    if audience == "client":
        require_client_permission(db, service, PermissionKey.VM_REINSTALL)
    fields = ssh_key_fields_for_service(db, service)
    return {
        **fields,
        "vm_template_id": service.vm.vm_template_id if service.vm else None,
        "reinstall_templates": list_reinstall_templates_for_service(db, service),
    }


@router.post("/services/{service_id}/vm/reinstall", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def billing_vm_reinstall(
    service_id: int,
    body: Optional[BillingVmReinstallBody] = None,
    audience: str = "admin",
    *,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """Destroy guest (if any) and reprovision at the reserved VMID (WHMCS / billing)."""
    from app.services.vm_reinstall_service import VmReinstallError, reinstall_vm_guest

    if audience not in ("admin", "client"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_AUDIENCE_ADMIN_OR_CLIENT_MSG)
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_NOT_VM_SERVICE_MSG)
    if audience == "client":
        require_client_permission(db, service, PermissionKey.VM_REINSTALL)

    payload = body or BillingVmReinstallBody()
    try:
        result = await reinstall_vm_guest(
            db,
            service,
            vm_template_id=payload.vm_template_id,
            ssh_public_keys=payload.ssh_public_keys,
        )
    except VmReinstallError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return result


@router.post("/services/{service_id}/ipmi-ticket", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def create_ipmi_ticket(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """
    Mint a one-time IPMI proxy launch ticket for a bare-metal service.

    WHMCS (or any billing integration) calls this on behalf of the already
    authenticated end user; the returned ``launch_url`` opens the BMC web UI via
    the Rackflow IPMI reverse proxy. No Rackflow login is required.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG
        )

    _assert_billing_owned_service(service, integration)
    require_client_permission(db, service, PermissionKey.BMS_IPMI)

    server = service_linked_server(db, service)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_NO_LINKED_SERVER_MSG,
        )

    try:
        payload = build_launch_payload(server)
    except IPMIProxyUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=exc.detail
        ) from exc

    logger.info(
        "Billing API: Minted IPMI ticket for service %s (server %s) via integration '%s'",
        service_id,
        server.id,
        integration.name,
    )
    return payload


@router.post("/services/{service_id}/vnc-ticket", status_code=status.HTTP_200_OK, response_model=VmVncTicketResponse, responses=COMMON_ERROR_RESPONSES)
async def create_vnc_ticket(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """
    Mint a one-time VM VNC console launch ticket.

    WHMCS (or any billing integration) calls this on behalf of the already
    authenticated end user; the returned ``launch_url`` opens Rackflow's
    ``/vnc`` page (typically in a popup), which redeems the ticket for a
    console session. No Rackflow login is required, and Proxmox account
    credentials never reach the browser.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG
        )

    _assert_billing_owned_service(service, integration)
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_NOT_VM_SERVICE_MSG)
    require_client_permission(db, service, PermissionKey.VM_CONSOLE)

    cid, _node, vmid = vm_placement(service)
    if cid is None or vmid is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="VM placement is not configured")

    try:
        token = mint_launch_ticket(service.id)
        launch_url = build_launch_url(token)
    except VmVncUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc

    logger.info(
        "Billing API: Minted VM VNC launch ticket for service %s via integration '%s'",
        service_id,
        integration.name,
    )
    return VmVncTicketResponse(launch_url=launch_url, expires_in=settings.vm_vnc_launch_ttl_seconds)


@router.post("/services/{service_id}/kvm-ticket", status_code=status.HTTP_200_OK, response_model=IpmiKvmTicketResponse, responses=COMMON_ERROR_RESPONSES)
async def create_kvm_ticket(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """Mint a one-time IPMI HTML5 KVM launch ticket.

    Gated by ``bms.kvm`` (separate from ``bms.ipmi``, which opens the BMC
    web UI via the IPMI reverse proxy). WHMCS (or any billing integration)
    calls this on behalf of the already authenticated end user; the
    returned ``launch_url`` opens Rackflow's ``/kvm`` page. BMC credentials
    never reach the browser.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG
        )

    _assert_billing_owned_service(service, integration)
    require_client_permission(db, service, PermissionKey.BMS_KVM)

    server = service_linked_server(db, service)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_NO_LINKED_SERVER_MSG,
        )
    if not kvm_ready(server):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="HTML5 KVM is not configured for this server",
        )

    try:
        token = mint_kvm_launch_ticket(server.id)
        launch_url = build_kvm_launch_url(token)
    except IpmiKvmTicketUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc

    logger.info(
        "Billing API: Minted IPMI KVM launch ticket for service %s (server %s) via integration '%s'",
        service_id,
        server.id,
        integration.name,
    )
    return IpmiKvmTicketResponse(launch_url=launch_url, expires_in=settings.ipmi_kvm_launch_ttl_seconds)


@router.post("/services/{service_id}/sol-ticket", status_code=status.HTTP_200_OK, response_model=SolTicketResponse, responses=COMMON_ERROR_RESPONSES)
async def create_sol_ticket(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """Mint a one-time Serial-over-LAN launch ticket.

    Gated by ``bms.sol``. The returned ``launch_url`` opens Rackflow's ``/sol``
    page. BMC credentials never reach the browser.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG
        )

    _assert_billing_owned_service(service, integration)
    require_client_permission(db, service, PermissionKey.BMS_SOL)

    server = service_linked_server(db, service)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_NO_LINKED_SERVER_MSG,
        )
    if not sol_ready(server):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Serial-over-LAN is not configured for this server",
        )

    try:
        token = mint_sol_launch_ticket(server.id)
        launch_url = build_sol_launch_url(token)
    except SolTicketUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc

    logger.info(
        "Billing API: Minted SOL launch ticket for service %s (server %s) via integration '%s'",
        service_id,
        server.id,
        integration.name,
    )
    return SolTicketResponse(launch_url=launch_url, expires_in=settings.sol_launch_ttl_seconds)


@router.post("/services/{service_id}/sol/send", status_code=status.HTTP_200_OK, response_model=SolSendResponse, responses=COMMON_ERROR_RESPONSES)
async def billing_sol_send(
    service_id: int,
    body: SolSendRequest,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """Write bytes into the linked server's SOL hub (billing / WHMCS)."""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG
        )
    _assert_billing_owned_service(service, integration)
    require_client_permission(db, service, PermissionKey.BMS_SOL)
    return await perform_sol_send(
        db,
        service_linked_server(db, service),
        body,
        source=_BILLING_SERVICE_LITERAL,
        service_id=service.id,
    )


@router.get("/services/{service_id}/virtual-media", status_code=status.HTTP_200_OK, response_model=VirtualMediaStatusResponse, responses=COMMON_ERROR_RESPONSES)
async def billing_get_virtual_media(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """BMC virtual CD status + ISO catalog for a billed service."""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    require_client_permission(db, service, PermissionKey.BMS_VIRTUAL_MEDIA)
    return await perform_status(service_linked_server(db, service), db=db)


@router.post("/services/{service_id}/virtual-media/insert", status_code=status.HTTP_200_OK, response_model=VirtualMediaStatusResponse, responses=COMMON_ERROR_RESPONSES)
async def billing_insert_virtual_media(
    service_id: int,
    body: VirtualMediaInsertRequest,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    require_client_permission(db, service, PermissionKey.BMS_VIRTUAL_MEDIA)
    return await perform_insert(
        db,
        service_linked_server(db, service),
        body.filename,
        boot_once=body.boot_once,
        source=_BILLING_SERVICE_LITERAL,
        service_id=service.id,
    )


@router.post("/services/{service_id}/virtual-media/eject", status_code=status.HTTP_200_OK, response_model=VirtualMediaStatusResponse, responses=COMMON_ERROR_RESPONSES)
async def billing_eject_virtual_media(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    require_client_permission(db, service, PermissionKey.BMS_VIRTUAL_MEDIA)
    return await perform_eject(
        db,
        service_linked_server(db, service),
        source=_BILLING_SERVICE_LITERAL,
        service_id=service.id,
    )


@router.post("/services/{service_id}/portal-sso", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def create_portal_sso_ticket(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """One-click client portal sign-in for the billing platform's own logged-in user.

    Mirrors ``/ipmi-ticket``: the billing platform calls this on behalf of the
    already-authenticated end user for one of their own services. If that
    billing identity has no linked Rackflow portal account yet, one is
    created automatically (no password; login stays SSO/impersonation-only
    until an admin sets one). Returns a one-time ``token`` to redeem at
    ``GET /api/client/sso/redeem?token=...``, which sets a normal client
    session cookie and redirects to ``/client``.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)

    _assert_billing_owned_service(service, integration)
    require_client_permission(db, service, PermissionKey.SERVICE_PORTAL)

    # _assert_billing_owned_service guarantees owner_user is set and billed.
    user = service.owner_user
    token = mint_sso_ticket(user.id)

    logger.info(
        "Billing API: minted portal SSO ticket for service %s (user %s) via integration '%s'",
        service_id,
        user.id,
        integration.name,
    )
    return {"token": token, "redeem_path": "/api/client/sso/redeem", "expires_in": settings.client_sso_ticket_ttl_seconds}


@router.get("/services/{service_id}/usage", response_model=ServerUsage, responses=COMMON_ERROR_RESPONSES)
async def get_service_usage(
    service_id: int,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    Get service resource usage/stats.
    
    Note: This is a placeholder - actual usage collection would need to be implemented
    based on your monitoring system.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG
        )
    
    _assert_billing_owned_service(service, integration)

    server = service_linked_server(db, service)
    if not server:
        return ServerUsage(
            server_id=None,
            ram_total_gb=None,
            disk_total_gb=None,
            cpu_usage_percent=None,
            ram_usage_gb=None,
            disk_usage_gb=None,
            network_rx_bytes=None,
            network_tx_bytes=None,
            uptime_seconds=None,
            last_updated=None,
        )

    disks = DiskDAO.get_by_server(db, server.id)
    total_disk_gb = sum(d.capacity_gb for d in disks) if disks else 0

    return ServerUsage(
        server_id=server.id,
        ram_total_gb=server.ram_gb,
        disk_total_gb=total_disk_gb,
        # Usage metrics would come from monitoring system
        cpu_usage_percent=None,
        ram_usage_gb=None,
        disk_usage_gb=None,
        network_rx_bytes=None,
        network_tx_bytes=None,
        uptime_seconds=None,
        last_updated=None
    )


@router.post("/services/{service_id}/actions/run-script", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def run_script_on_service(
    service_id: int,
    action: ServiceActionRunScript,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    Run a script on a service's server.
    
    Only scripts marked as user_executable can be run via this endpoint.
    """
    logger.info(f"Billing API: Running script {action.script_id} on service {service_id} via integration '{integration.name}'")
    
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG
        )
    
    _assert_billing_owned_service(service, integration)
    require_client_permission(db, service, PermissionKey.BMS_RUN_SCRIPT)
    
    # Get script
    script = ScriptDAO.get_by_id(db, action.script_id)
    if not script:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Script not found"
        )
    
    # Verify script is enabled and user-executable
    if not script.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Script is disabled"
        )
    
    if not script.user_executable:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Script is not available for external users"
        )

    server = service_linked_server(db, service)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="PXE/script actions require a bare-metal RackFlow server; not available for VM-only services",
        )
    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.INSTALL,
        action="run_script",
        source="billing_api",
        message=f"Run script request for '{script.name}'",
        details={
            "service_id": service.id,
            "integration_id": integration.id,
            "script_id": script.id,
            "script_name": script.name,
        },
    )
    
    try:
        # Get script content and replace variables
        script_content = script.content

        # Get server's PXE boot port MAC address
        pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server.id)
        server_mac = pxe_port.mac_address if pxe_port else None

        # Build replacements
        replacements = {
            "SERVER_IP": server.server_ip or "",
            "SERVER_MAC": server_mac or "",
            "SERVER_ID": str(server.id),
        }

        # Add custom parameters
        if action.parameters:
            for param_name, param_value in action.parameters.items():
                replacements[param_name.upper()] = str(param_value)

        # Replace variables in script
        for var_name, var_value in replacements.items():
            script_content = script_content.replace(f"${{{var_name}}}", var_value)
            script_content = script_content.replace(f"${var_name}", var_value)

        # Get temp OS config for debian-live
        temp_os_service = get_temp_os_service()
        os_config = temp_os_service.get_os_config("debian-live")
        if not os_config:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="debian-live temporary OS not found",
            )

        # Get URLs using the same IP that DHCP advertises as next-server for this client's PXE IP
        base_url = _get_base_url_for_pxe_ip(db, pxe_port.pxe_ip if pxe_port else None)
        kernel_url = temp_os_service.get_kernel_url("debian-live", base_url)
        initrd_url = temp_os_service.get_initrd_url("debian-live", base_url)
        kernel_params = temp_os_service.get_kernel_params("debian-live")

        # Add squashfs fetch URL
        squashfs_url = temp_os_service.get_squashfs_url("debian-live", base_url)
        if squashfs_url and "fetch=" not in kernel_params:
            kernel_params = f"{kernel_params} fetch={squashfs_url}"

        # Add preseed URL to kernel params
        if server_mac:
            preseed_url = f"{base_url}/api/servers/interaction/preseed?mac={server_mac}"
            kernel_params = f"{kernel_params} preseed/url={preseed_url}"

        # Create boot task
        boot_task = BootTaskDAO.create(
            db,
            server_id=server.id,
            boot_type=BootType.TEMP_OS,
            temp_os_id="debian-live",
            kernel_url=kernel_url,
            initrd_url=initrd_url,
            kernel_params=kernel_params,
            script_content=script_content,
            description=f"Run script '{script.name}' via billing API",
            status=BootTaskStatus.PENDING
        )

        # Add script URL to kernel params after boot task creation (for debian-live)
        if boot_task.script_content:
            boot_task.kernel_params = _inject_script_url_param(
                boot_task.kernel_params, base_url, boot_task.id
            )
            db.commit()
            db.refresh(boot_task)
    except HTTPException as exc:
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.INSTALL,
            action="run_script",
            source="billing_api",
            message=f"Run script request failed for '{script.name}'",
            details={
                "service_id": service.id,
                "integration_id": integration.id,
                "script_id": script.id,
                "detail": exc.detail,
            },
        )
        raise
    except Exception as exc:
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.INSTALL,
            action="run_script",
            source="billing_api",
            message=f"Run script request failed for '{script.name}'",
            details={
                "service_id": service.id,
                "integration_id": integration.id,
                "script_id": script.id,
            },
            error=exc,
        )
        raise

    log_server_activity_success(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.INSTALL,
        action="run_script",
        source="billing_api",
        message=f"Queued script '{script.name}'",
        details={
            "service_id": service.id,
            "integration_id": integration.id,
            "script_id": script.id,
            "boot_task_id": boot_task.id,
        },
    )
    logger.info(f"Billing API: Created boot task {boot_task.id} for script '{script.name}' on service {service_id}")

    return {
        "status": "success",
        "message": f"Script '{script.name}' queued for execution",
        "boot_task_id": boot_task.id
    }


@router.post("/services/{service_id}/actions/reinstall-os", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def reinstall_os_on_service(
    service_id: int,
    action: ServiceActionReinstallOS,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    Reinstall OS on a service's server.

    When the service was provisioned from a server group, the template must be
    in that group's permitted OS list. Otherwise only ``user_reinstallable``
    templates are allowed.
    """
    logger.info(f"Billing API: Reinstalling OS '{action.template_id}' on service {service_id} via integration '{integration.name}'")
    
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_SERVICE_NOT_FOUND_MSG
        )
    
    _assert_billing_owned_service(service, integration)
    require_client_permission(db, service, PermissionKey.BMS_REINSTALL)

    server = service_linked_server(db, service)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="OS reinstall via PXE requires a bare-metal RackFlow server; not available for VM-only services",
        )
    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.INSTALL,
        action="reinstall_os",
        source="billing_api",
        message=f"OS reinstall requested for template '{action.template_id}'",
        details={
            "service_id": service.id,
            "integration_id": integration.id,
            "template_id": action.template_id,
        },
    )

    try:
        # Get template
        template_service = get_template_service()
        template = template_service.get_template(action.template_id)
        if not template:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"OS template '{action.template_id}' not found"
            )

        group = _service_server_group(db, service)
        if group is not None:
            _determine_template_for_group(db, group.id, action.template_id)
        elif not template.user_reinstallable:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"OS template '{action.template_id}' is not available for user reinstallation"
            )

        template_parameters = resolve_template_parameters(template, action.template_parameters)
        _persist_resolved_template_params(service, action.template_id, template_parameters)

        # Get template script
        script_path = template_service.get_template_script_path(action.template_id)
        if not script_path or not script_path.exists():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Template '{action.template_id}' missing installation script"
            )

        # Read script content
        script_content = await asyncio.to_thread(script_path.read_text, encoding="utf-8")

        # Get server's PXE boot port MAC address
        pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server.id)
        server_mac = pxe_port.mac_address if pxe_port else None

        # Get OS disk for installation
        os_disk = DiskDAO.get_os_disk(db, server.id)

        # Build replacements
        replacements = {
            "SERVER_IP": server.server_ip or "",
            "SERVER_MAC": server_mac or "",
            "SERVER_ID": str(server.id),
        }

        # Add disk information
        if os_disk:
            replacements["OS_DISK_SERIAL"] = os_disk.serial_number or ""
            replacements["OS_DISK_SIZE_GB"] = str(os_disk.capacity_gb)
            replacements["OS_DISK_TYPE"] = os_disk.type.value.lower() if hasattr(os_disk.type, 'value') else str(os_disk.type).lower()
        else:
            replacements["OS_DISK_SERIAL"] = ""
            replacements["OS_DISK_SIZE_GB"] = ""
            replacements["OS_DISK_TYPE"] = ""

        # Disk image filename will be set after token generation
        disk_image_filename = None
        if template.disk_image:
            disk_image_filename = template.disk_image.split("/")[-1]

        # Scope the eventual download token to this template's own files,
        # bound to this specific template_id (never a global "*"), so it
        # can't be replayed to read arbitrary scripts/templates/ISOs
        # elsewhere in the system, or against a same-named file in a
        # different template.
        template_allowed_files: list[str] = []
        if template.template_dir:
            for rel_path in template_service.enumerate_relative_files(action.template_id):
                template_allowed_files.append(template_file_scope(action.template_id, rel_path))

        # Add template parameters. Escape for double-quoted shell-string
        # context (see _queue_template_install_for_service) since these are
        # customer-supplied values embedded via literal text substitution
        # into a root-executed install script.
        if template_parameters:
            for param_name, param_value in template_parameters.items():
                replacements[f"PARAM_{param_name.upper()}"] = shell_escape_double_quoted(param_value)

        # Template installations use debian-live
        temp_os_service = get_temp_os_service()
        # Use DHCP-configured next-server IP for this server's PXE subnet
        base_url = _get_base_url_for_pxe_ip(db, pxe_port.pxe_ip if pxe_port else None)
        kernel_url = temp_os_service.get_kernel_url("debian-live", base_url)
        initrd_url = temp_os_service.get_initrd_url("debian-live", base_url)
        kernel_params = temp_os_service.get_kernel_params("debian-live")

        # Add squashfs fetch URL
        squashfs_url = temp_os_service.get_squashfs_url("debian-live", base_url)
        if squashfs_url and "fetch=" not in kernel_params:
            kernel_params = f"{kernel_params} fetch={squashfs_url}"

        # Create boot task
        boot_task = BootTaskDAO.create(
            db,
            server_id=server.id,
            boot_type=BootType.TEMP_OS,
            temp_os_id="debian-live",
            kernel_url=kernel_url,
            initrd_url=initrd_url,
            kernel_params=kernel_params,
            script_content=script_content,
            description=f"Reinstall OS '{template.name}' via billing API",
            status=BootTaskStatus.PENDING
        )

        # Generate download token scoped to this template's own files (never
        # a global "*").
        allowed_files = list(template_allowed_files)
        if disk_image_filename:
            allowed_files.append(disk_image_filename)
        download_token_service = get_download_token_service()
        download_token = download_token_service.generate_token(
            boot_task_id=boot_task.id,
            allowed_files=allowed_files if allowed_files else None,
            expires_in=3600,
        )

        # Inject token into script and update URLs
        if download_token:
            replacements["DOWNLOAD_TOKEN"] = download_token
            replacements["API_BASE_URL"] = base_url
            replacements["TEMPLATE_ID"] = action.template_id
            if disk_image_filename:
                disk_image_url = f"{base_url}/api/servers/interaction/disk-images/{disk_image_filename}?token={download_token}"
                replacements["DISK_IMAGE_URL"] = disk_image_url

            # Re-apply replacements including DOWNLOAD_TOKEN
            for var_name, var_value in replacements.items():
                script_content = script_content.replace(f"${{{var_name}}}", var_value)
                script_content = script_content.replace(f"${var_name}", var_value)

            boot_task.script_content = script_content
            db.commit()
            db.refresh(boot_task)

        # Add script URL to kernel params after boot task creation (for debian-live)
        if boot_task.script_content:
            boot_task.kernel_params = _inject_script_url_param(
                boot_task.kernel_params, base_url, boot_task.id
            )
            db.commit()
            db.refresh(boot_task)
    except HTTPException as exc:
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.INSTALL,
            action="reinstall_os",
            source="billing_api",
            message=f"OS reinstall failed for template '{action.template_id}'",
            details={
                "service_id": service.id,
                "integration_id": integration.id,
                "template_id": action.template_id,
                "detail": exc.detail,
            },
        )
        raise
    except Exception as exc:
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.INSTALL,
            action="reinstall_os",
            source="billing_api",
            message=f"OS reinstall failed for template '{action.template_id}'",
            details={
                "service_id": service.id,
                "integration_id": integration.id,
                "template_id": action.template_id,
            },
            error=exc,
        )
        raise

    log_server_activity_success(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.INSTALL,
        action="reinstall_os",
        source="billing_api",
        message=f"Queued OS reinstall '{template.name}'",
        details={
            "service_id": service.id,
            "integration_id": integration.id,
            "template_id": action.template_id,
            "boot_task_id": boot_task.id,
        },
    )
    logger.info(f"Billing API: Created boot task {boot_task.id} for OS reinstall '{template.name}' on service {service_id}")

    return {
        "status": "success",
        "message": f"OS reinstallation '{template.name}' queued",
        "boot_task_id": boot_task.id
    }


# Registered after static /actions/run-script and /actions/reinstall-os so those
# BMS endpoints are not swallowed by the {action_name} path parameter.
@router.post("/services/{service_id}/actions/{action_name}", status_code=status.HTTP_200_OK, responses=COMMON_ERROR_RESPONSES)
async def billing_run_strategy_action(
    service_id: int,
    action_name: str,
    body: StrategyActionRequest,
    audience: str = "client",
    *,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND_MSG)
    _assert_billing_owned_service(service, integration)
    if audience not in ("admin", "client"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_AUDIENCE_ADMIN_OR_CLIENT_MSG)
    try:
        return await run_action(db, service, action_name, body.params, audience)  # type: ignore[arg-type]
    except StrategyActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc) or "Strategy action failed",
        ) from exc


def _billing_product_effective_specs(db: Session, product, family) -> dict:
    effective_specs = ProductVMConfigDAO.resolve_effective_config(db, product)
    if effective_specs:
        return effective_specs
    return {
        **((family.defaults if family else None) or {}),
        **(product.overrides or {}),
    }


def _billing_product_vm_templates(product) -> list[dict]:
    from app.services.ssh_public_keys import os_type_accepts_ssh_key
    from app.services.vm_install_type_strategy import resolve_vm_template_strategy

    templates = []
    for mapping in product.vm_template_mappings or []:
        tmpl = mapping.vm_template
        if tmpl is None or not tmpl.enabled:
            continue
        strategy_name = None
        try:
            strategy_name = resolve_vm_template_strategy(tmpl.os_type).get("strategy_name")
        except ValueError:
            strategy_name = None
        templates.append(
            {
                "id": tmpl.id,
                "code": tmpl.code,
                "name": tmpl.name,
                "os_type": tmpl.os_type,
                "proxmox_template_name": tmpl.proxmox_template_name,
                "strategy_name": strategy_name,
                "accepts_ssh_key": os_type_accepts_ssh_key(tmpl.os_type),
            }
        )
    templates.sort(key=lambda row: (row.get("name") or "").lower())
    return templates


def _billing_product_checkout_os_mode(family_type, vm_templates: list) -> str:
    if vm_templates:
        return "vm_template"
    if family_type == "bare_metal":
        return "server_group"
    return "none"


def _billing_product_catalog_item(db: Session, product) -> dict:
    """Serialize a catalog product for WHMCS module settings / checkout sync."""
    family = product.family
    family_type = family.service_type if family is not None else None
    effective_specs = _billing_product_effective_specs(db, product, family)
    vm_templates = _billing_product_vm_templates(product)
    checkout_os_mode = _billing_product_checkout_os_mode(family_type, vm_templates)

    return {
        "id": product.id,
        "code": product.code,
        "name": product.name,
        "description": product.description or "",
        "enabled": bool(product.enabled),
        "service_type": family.service_type if family else None,
        "family": (
            {
                "id": family.id,
                "code": family.code,
                "name": family.name,
                "service_type": family.service_type,
                "provisioning_backend": family.provisioning_backend,
            }
            if family
            else None
        ),
        "effective_specs": effective_specs or {},
        "overrides": product.overrides or {},
        "vm_templates": vm_templates,
        "checkout_os_mode": checkout_os_mode,
    }


@router.get("/products", response_model=List[dict], responses=COMMON_ERROR_RESPONSES)
async def list_products_billing(
    service_type: Optional[str] = None,
    include_disabled: bool = False,
    *,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """
    List RackFlow catalog products for billing systems (WHMCS Module Settings).

    Optional ``service_type`` filters to ``bare_metal``, ``vm``, or ``http_proxy``.
    Each item includes effective specs and linked VM templates so the admin UI
    can preview what a product will provision. Bare-metal products use
    ``checkout_os_mode=server_group``; installable OS comes from the selected
    server group.
    """
    rows = ProductDAO.get_all(db)
    wanted = (service_type or "").strip().lower() or None
    out: List[dict] = []
    for product in rows:
        if not include_disabled and not product.enabled:
            continue
        family = product.family
        if wanted and (family is None or family.service_type != wanted):
            continue
        out.append(_billing_product_catalog_item(db, product))
    return out


@router.get("/products/{product_code}", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def get_product_billing(
    product_code: str,
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """Return one catalog product (by code) with OS/template preview payload."""
    product = ProductDAO.get_by_code(db, product_code)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown product_code '{product_code}'",
        )
    return _billing_product_catalog_item(db, product)


@router.get("/isos", response_model=List[dict], responses=COMMON_ERROR_RESPONSES)
async def list_isos_billing(
    integration: BillingIntegrationDep,
):
    """
    List ISO files available for boot (read-only).
    Returns filenames for use in product configuration.
    """
    del integration
    return catalog_for_billing()


@router.get("/temp-os", response_model=List[dict], responses=COMMON_ERROR_RESPONSES)
async def list_temp_os_billing(
    integration: BillingIntegrationDep,
):
    """
    List temporary OS configurations (e.g. debian-live) for product configuration.
    """
    temp_os_service = get_temp_os_service()
    configs = temp_os_service.scan_os_configs()
    return [
        {
            "id": c.id,
            "name": c.name,
            "description": c.description or "",
        }
        for c in configs
    ]


@router.get("/proxmox/clusters", response_model=List[dict], responses=COMMON_ERROR_RESPONSES)
async def list_proxmox_clusters_billing(
    integration: BillingIntegrationDep,
    db: DbDep,
):
    """
    List enabled Proxmox clusters (locations) for WHMCS module/config option loaders.

    Returns id + display name + enabled nodes so billing can offer a location
    dropdown without an admin session.
    """
    from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO

    out: List[dict] = []
    for cluster in ProxmoxInventoryDAO.list_clusters(db):
        if not cluster.enabled:
            continue
        nodes = [
            {"node_name": n.node_name, "enabled": bool(n.enabled)}
            for n in (cluster.nodes or [])
            if n.enabled
        ]
        out.append(
            {
                "id": cluster.id,
                "name": cluster.name,
                "nodes": nodes,
            }
        )
    return out


@router.get("/server-groups", response_model=List[dict], responses=COMMON_ERROR_RESPONSES)
async def list_server_groups_billing(
    skip: int = 0,
    limit: int = 100,
    *,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    List server groups via billing API (read-only).

    Allows billing systems (e.g. WHMCS) to populate the RackFlow Server Group
    dropdown when using a billing API key for the server connection test.
    """
    server_groups = ServerGroupDAO.get_all(db, skip=skip, limit=limit)
    return [
        {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "server_count": len(group.servers) if group.servers else 0,
            "created_at": group.created_at.isoformat() if group.created_at else None,
            "updated_at": group.updated_at.isoformat() if group.updated_at else None,
            "enable_isos": getattr(group, "enable_isos", False) or False,
            "permitted_isos": list(group.permitted_isos or []),
            "enable_temp_os": getattr(group, "enable_temp_os", False) or False,
            "permitted_temp_os": list(group.permitted_temp_os or []),
            "enable_scripts": getattr(group, "enable_scripts", False) or False,
            "permitted_scripts": list(group.permitted_scripts or []),
            "enable_os_templates": getattr(group, "enable_os_templates", False) or False,
            "permitted_os_templates": list(group.permitted_os_templates or []),
            "os_templates": resolved_os_templates_for_ids(group.permitted_os_templates),
        }
        for group in server_groups
    ]


@router.get("/scripts", response_model=List[dict], responses=COMMON_ERROR_RESPONSES)
async def list_available_scripts(
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    List scripts available for execution via billing API.
    
    Only returns scripts marked as user_executable.
    """
    scripts = ScriptDAO.get_all(db, enabled_only=True, user_executable_only=True)
    
    return [
        {
            "id": script.id,
            "name": script.name,
            "description": script.description
        }
        for script in scripts
    ]


@router.get("/os-templates", response_model=List[dict], responses=COMMON_ERROR_RESPONSES)
async def list_available_os_templates(
    service_id: Optional[int] = None,
    *,
    integration: BillingIntegrationDep,
    db: DbDep
):
    """
    List OS templates available for reinstallation via billing API.

    Without ``service_id``, only templates marked ``user_reinstallable``.
    With ``service_id``, return the service's server-group permitted templates
    (the same list used at checkout) when OS templates are enabled on the group.
    """
    if service_id is not None:
        service = ServiceDAO.get_by_id(db, service_id)
        if not service:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_SERVICE_NOT_FOUND_MSG,
            )
        _assert_billing_owned_service(service, integration)
        group = _service_server_group(db, service)
        if group is None or not (getattr(group, "enable_os_templates", False) or False):
            return []
        return resolved_os_templates_for_ids(group.permitted_os_templates)

    template_service = get_template_service()
    templates = template_service.get_all_templates()
    return [
        serialize_os_template(template)
        for template in templates
        if template.user_reinstallable
    ]
