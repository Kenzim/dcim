from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.dao.service_dao import ServiceDAO
from app.models.service import ServiceStatus, ServiceType
from app.services.service_resource import service_linked_server, vm_placement
from app.services.ipmi_ticket_service import build_launch_payload, IPMIProxyUnavailable
from app.services.proxmox_placement import ProxmoxPlacementError, resolve_proxmox_plugin_for_service
from app.services.vm_guest_credentials import session_guest_fields
from app.services.vm_vnc_ticket_service import (
    build_relative_error_url,
    build_relative_launch_url,
    mint_launch_ticket,
    mint_ws_session,
)
from app.services.client_permission_resolver import (
    require_client_permission,
    resolve_client_permissions,
)
from app.core.client_permissions import PermissionKey
from app.dao.installation_task_dao import InstallationTaskDAO
from app.dao.ipam_dao import IPAMDAO
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_success,
    log_server_activity_failure,
)
from app.models.server_activity import ServerActivityEventType
from app.schemas.billing import PowerAction
from app.services.proxy_credentials import generate_proxy_password, generate_proxy_username
from app.services.proxy_provisioning import assignment_payload
from app.plugins.registry import get_registry
from app.plugins.base import PowerState
from app.plugins.proxmox import ConsoleTypeUnavailable
from app.schemas.vm_vnc import VmConsoleTypesResponse, VmVncSessionResponse
from app.api.vm_backup_routes import BackupCreateBody, BackupMutateBody, map_backup_error
from app.services.vm_backup_service import (
    create_client_backup,
    delete_client_backup,
    list_service_backups_and_jobs,
    restore_service_backup,
)
from app.models.service_vm import VMGuestState
from app.services.vm_strategy_executor import provision_vm_service_async
import asyncio
import logging

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/services", tags=["services-client"])


class ClientServiceResponse(BaseModel):
    id: int
    name: str
    service_type: Optional[str] = None
    status: str
    external_service_id: Optional[str] = None
    description: Optional[str] = None
    product_code: Optional[str] = None
    os_code: Optional[str] = None
    proxmox_cluster_id: Optional[int] = None
    proxmox_node_name: Optional[str] = None
    proxmox_vmid: Optional[int] = None
    vm_template_id: Optional[int] = None
    accepts_ssh_key: bool = False
    has_ssh_public_keys: bool = False
    ssh_public_keys_text: str = ""
    needs_ssh_key_prompt: bool = False
    primary_ip: Optional[str] = None
    power_state: str = "unknown"
    power_available: bool = False


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


def _service_to_client_response(service, db: Optional[Session] = None) -> ClientServiceResponse:
    cid, node, vmid = vm_placement(service)
    ssh_fields = {
        "accepts_ssh_key": False,
        "has_ssh_public_keys": False,
        "ssh_public_keys_text": "",
        "needs_ssh_key_prompt": False,
    }
    if db is not None and service.service_type == ServiceType.VM:
        from app.services.ssh_public_keys import ssh_key_fields_for_service

        ssh_fields = ssh_key_fields_for_service(db, service)
    return ClientServiceResponse(
        id=service.id,
        name=service.name,
        service_type=service.service_type.value if service.service_type else None,
        status=service.status.value if isinstance(service.status, ServiceStatus) else str(service.status),
        external_service_id=service.external_service_id,
        description=service.description,
        product_code=service.product_code,
        os_code=service.os_code,
        proxmox_cluster_id=cid,
        proxmox_node_name=node,
        proxmox_vmid=vmid,
        vm_template_id=service.vm.vm_template_id if service.vm else None,
        accepts_ssh_key=bool(ssh_fields.get("accepts_ssh_key")),
        has_ssh_public_keys=bool(ssh_fields.get("has_ssh_public_keys")),
        ssh_public_keys_text=str(ssh_fields.get("ssh_public_keys_text") or ""),
        needs_ssh_key_prompt=bool(ssh_fields.get("needs_ssh_key_prompt")),
    )


def _power_permission_key(service) -> str:
    """The client permission key gating power actions for this service type."""
    return PermissionKey.VM_POWER if service.service_type == ServiceType.VM else PermissionKey.BMS_POWER


async def _client_plugin_instance(db: Session, service):
    """Return ``(plugin, server_or_none)`` for an owned service.

    VM services resolve the Proxmox plugin from placement (no linked Server
    row by design), treating the cached node as a hint and searching the
    cluster for the VMID's current node if it's missing/stale; bare-metal /
    http_proxy resolve the linked server's plugin. Raises HTTPException with
    client-safe messages when power/IPMI control isn't applicable.
    """
    if service.service_type == ServiceType.VM:
        try:
            plugin, _cid, _node, _vmid = await resolve_proxmox_plugin_for_service(db, service)
        except ProxmoxPlacementError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return plugin, None
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
            detail="Service has no linked server",
        )
    return get_registry().get_plugin(server.plugin_name, server.plugin_config), server


async def _best_effort_power_state(db: Session, service) -> PowerState:
    """Live power state; UNKNOWN whenever the plugin can't be built/reached."""
    try:
        plugin, _ = await _client_plugin_instance(db, service)
        state = await plugin.get_power_state()
    except Exception:
        return PowerState.UNKNOWN
    return state if isinstance(state, PowerState) else PowerState.UNKNOWN


def _client_primary_ip(db: Session, service) -> Optional[str]:
    """Primary IP for portal cards: VM IP allocation/config, linked server
    IP, or the first IPAM assignment for server-less proxy services."""
    if service.service_type == ServiceType.VM:
        if service.vm:
            if service.vm.vm_ip_allocation:
                return service.vm.vm_ip_allocation.ip_address
            return (service.config or {}).get("vm_ip_address")
        return None
    server = service_linked_server(db, service)
    if server:
        return server.server_ip
    if service.service_type == ServiceType.HTTP_PROXY:
        assignments = IPAMDAO.get_assignment_by_service(db, service.id)
        if assignments:
            return assignments[0].ip_address
    return None


@router.get("/me", response_model=List[ClientServiceResponse])
async def list_my_services(
    service_type: Optional[str] = None,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    services = ServiceDAO.get_by_owner_user(db, int(user_id))
    if service_type:
        try:
            st = ServiceType(service_type.lower())
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid service_type") from exc
        services = [s for s in services if s.service_type == st]
    responses = [_service_to_client_response(s, db) for s in services]
    # Live power states, concurrently — a slow/unreachable BMC or cluster
    # degrades that one card to "unknown" instead of failing the list.
    power_states = await asyncio.gather(
        *[_best_effort_power_state(db, s) for s in services],
        return_exceptions=True,
    )
    for resp, service, state in zip(responses, services, power_states):
        resp.primary_ip = _client_primary_ip(db, service)
        resp.power_state = state.value if isinstance(state, PowerState) else PowerState.UNKNOWN.value
        # Card-level power gating: permission granted and (for non-VM) a
        # linked server exists. VM power goes through Proxmox placement.
        resp.power_available = bool(
            resolve_client_permissions(db, service).get(_power_permission_key(service), False)
            and (service.service_type == ServiceType.VM or service_linked_server(db, service) is not None)
        )
    return responses


@router.get("/{service_id}")
async def client_get_service(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Owner-scoped service detail for the portal.

    Base list fields plus live power state, primary IP, availability flags,
    and the effective client permission map so the SPA can hide actions
    instead of probing for 403s.
    """
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = ServiceDAO.get_by_id(db, service_id)
    # Only expose services owned by the logged-in user; hide others as 404.
    if not service or service.owner_user_id != int(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    permissions = resolve_client_permissions(db, service)
    server = service_linked_server(db, service)
    cid, node, vmid = vm_placement(service)
    power_state = await _best_effort_power_state(db, service)
    is_vm = service.service_type == ServiceType.VM
    is_proxy = service.service_type == ServiceType.HTTP_PROXY

    ipmi_available = bool(
        server
        and getattr(server, "ipmi_proxy_enabled", False)
        and getattr(server, "ipmi_web_management_url", None)
        and permissions.get(PermissionKey.BMS_IPMI, False)
    )
    console_available = bool(
        is_vm
        and cid is not None
        and node
        and str(node).strip()
        and vmid is not None
        and permissions.get(PermissionKey.VM_CONSOLE, False)
    )
    backups_available = bool(
        is_vm
        and cid is not None
        and vmid is not None
        and permissions.get(PermissionKey.VM_BACKUPS, False)
    )

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

    detail = _service_to_client_response(service, db).model_dump()
    detail.update(
        {
            "primary_ip": _client_primary_ip(db, service),
            "power_state": power_state.value,
            "server_name": server.name if server else None,
            "server_enabled": server.enabled if server else None,
            "guest_state": (
                service.vm.guest_state.value
                if is_vm and service.vm and service.vm.guest_state
                else None
            ),
            "permissions": permissions,
            # VM power goes through Proxmox placement (no linked Server row);
            # bare_metal/http_proxy power requires an actual linked server.
            "power_available": bool(
                permissions.get(_power_permission_key(service), False)
                and (is_vm or server is not None)
            ),
            "ipmi_available": ipmi_available,
            "ipmi_viewer_username": getattr(server, "ipmi_viewer_username", None) if ipmi_available else None,
            "ipmi_viewer_password": getattr(server, "ipmi_viewer_password", None) if ipmi_available else None,
            "console_available": console_available,
            "backups_available": backups_available,
            "proxy_credentials_available": bool(
                is_proxy and permissions.get(PermissionKey.PROXY_VIEW_CREDENTIALS, False)
            ),
            "proxy_rotate_available": bool(
                is_proxy and permissions.get(PermissionKey.PROXY_ROTATE_CREDENTIALS, False)
            ),
            "installation": installation,
        }
    )
    return detail


@router.post("/{service_id}/power")
async def client_power_service(
    service_id: int,
    body: PowerAction,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Power on/off/reboot/reset for a service the caller owns.

    Mirrors the billing power guard logic (suspended/terminated services and
    administratively disabled servers may only be powered off), gated by the
    ``vm.power`` / ``bms.power`` client permission.
    """
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.owner_user_id != int(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    require_client_permission(db, service, _power_permission_key(service))

    action = (body.action or "").lower()
    if action not in ("on", "off", "reboot", "reset"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid power action: {action}. Must be 'on', 'off', 'reboot', or 'reset'",
        )

    server = service_linked_server(db, service)
    # Any action that can leave the machine running (on/reboot/reset) is
    # forbidden for suspended/terminated services and disabled servers; only
    # "off" stays allowed so the box can always be powered down.
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

    plugin, _ = await _client_plugin_instance(db, service)

    log_kw = {"server_id": server.id} if server else {"service_id": service.id}
    log_server_activity_attempt(
        db,
        **log_kw,
        event_type=ServerActivityEventType.POWER,
        action=action,
        source="client_portal",
        message=f"Power action '{action}' requested",
        details={"service_id": service.id, "user_id": int(user_id)},
    )
    try:
        if action == "on":
            success = await plugin.power_on()
        elif action == "off":
            success = await plugin.power_off(force=False)
        else:  # reboot / reset
            success = await plugin.power_reset()
    except NotImplementedError as exc:
        log_server_activity_failure(
            db,
            **log_kw,
            event_type=ServerActivityEventType.POWER,
            action=action,
            source="client_portal",
            message=f"Power action '{action}' failed",
            details={"service_id": service.id, "user_id": int(user_id)},
            error=exc,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server plugin does not support power control",
        ) from exc
    except Exception as exc:
        logger.error(f"Client portal: Power action failed: {exc}", exc_info=True)
        log_server_activity_failure(
            db,
            **log_kw,
            event_type=ServerActivityEventType.POWER,
            action=action,
            source="client_portal",
            message=f"Power action '{action}' failed",
            details={"service_id": service.id, "user_id": int(user_id)},
            error=exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Power action failed: {str(exc)}",
        ) from exc
    if not success:
        log_server_activity_failure(
            db,
            **log_kw,
            event_type=ServerActivityEventType.POWER,
            action=action,
            source="client_portal",
            message=f"Power action '{action}' failed",
            details={"service_id": service.id, "user_id": int(user_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Power action command failed",
        )

    log_server_activity_success(
        db,
        **log_kw,
        event_type=ServerActivityEventType.POWER,
        action=action,
        source="client_portal",
        message=f"Power action '{action}' completed",
        details={"service_id": service.id, "user_id": int(user_id)},
    )
    return {"status": "success", "action": action, "message": f"Server power {action} command executed"}


@router.post("/{service_id}/ipmi-ticket")
async def create_ipmi_ticket(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mint a one-time IPMI proxy launch ticket for a service the caller owns."""
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")

    service = ServiceDAO.get_by_id(db, service_id)
    # Only expose services owned by the logged-in user; hide others as 404.
    if not service or service.owner_user_id != int(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    require_client_permission(db, service, PermissionKey.BMS_IPMI)

    server = service_linked_server(db, service)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service has no linked server")

    try:
        return build_launch_payload(server)
    except IPMIProxyUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc


def _client_owned_proxy_service(db: Session, service_id: int, user_id, permission: str):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.owner_user_id != int(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.service_type != ServiceType.HTTP_PROXY:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a proxy service")
    require_client_permission(db, service, permission)
    return service


@router.get("/{service_id}/proxy/credentials")
async def client_get_proxy_credentials(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List assigned proxy IP(s) + credentials + ready-to-use URLs for a service the caller owns."""
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = _client_owned_proxy_service(db, service_id, user_id, PermissionKey.PROXY_VIEW_CREDENTIALS)
    assignments = IPAMDAO.get_assignment_by_service(db, service.id)
    return {"assignments": [assignment_payload(a) for a in assignments]}


@router.post("/{service_id}/proxy/rotate")
async def client_rotate_proxy_credentials(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Rotate credentials (new username+password, same IP(s)) for a service the caller owns."""
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = _client_owned_proxy_service(db, service_id, user_id, PermissionKey.PROXY_ROTATE_CREDENTIALS)
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
            rotated_by=f"client:{user_id}",
        )
        if updated:
            rotated.append(assignment_payload(updated))
    return {"assignments": rotated}


async def _client_owned_vm_plugin(db: Session, service_id: int, user_id):
    """Shared owner/type/permission/placement checks for the VM console
    endpoints below; returns ``(service, plugin, cid, node, vmid)``."""
    service = ServiceDAO.get_by_id(db, service_id)
    # Only expose services owned by the logged-in user; hide others as 404.
    if not service or service.owner_user_id != int(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a VM service")

    require_client_permission(db, service, PermissionKey.VM_CONSOLE)

    try:
        plugin, cid, node, vmid = await resolve_proxmox_plugin_for_service(db, service)
    except ProxmoxPlacementError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return service, plugin, cid, node, vmid


@router.get("/{service_id}/vm/console-types", response_model=VmConsoleTypesResponse)
async def get_vnc_console_types(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Report which console types (noVNC/serial) this VM actually supports,
    for a VM service the caller owns. Fetched by the client portal UI before
    showing the "Open Console" control(s)."""
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")

    _service, plugin, _cid, _node, _vmid = await _client_owned_vm_plugin(db, service_id, user_id)
    try:
        available = await plugin.get_available_console_types()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not reach Proxmox: {exc}"
        ) from exc
    return VmConsoleTypesResponse(**available)


@router.post("/{service_id}/vm/vnc-session", response_model=VmVncSessionResponse)
async def create_vnc_session(
    service_id: int,
    console_type: Optional[str] = None,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mint a VNC/serial console session for a VM service the caller owns.

    ``console_type`` (``"vnc"``/``"serial"``) picks a specific type when the
    VM supports both; omit it to use the default preference (noVNC first).
    """
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")

    service, plugin, cid, node, vmid = await _client_owned_vm_plugin(db, service_id, user_id)

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
        service.id, cid, str(node).strip(), int(vmid), console["port"], console["ticket"], console["console_type"]
    )
    return VmVncSessionResponse(
        ws_token=session["ws_token"],
        ws_path="/api/vnc/ws",
        vnc_password=console["ticket"],
        expires_in=session["expires_in"],
        console_type=console["console_type"],
        **session_guest_fields(service),
    )


class ClientStrategyActionBody(BaseModel):
    params: Dict[str, Any] = Field(default_factory=dict)


@router.get("/{service_id}/actions")
async def client_list_strategy_actions(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.strategy_actions import list_actions

    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.owner_user_id != int(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return {"actions": list_actions(db, service, "client")}


@router.post("/{service_id}/actions/{action_name}")
async def client_run_strategy_action(
    service_id: int,
    action_name: str,
    body: ClientStrategyActionBody,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.strategy_actions import StrategyActionError, run_action

    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.owner_user_id != int(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    try:
        return await run_action(db, service, action_name, body.params, "client")
    except StrategyActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc) or "Strategy action failed",
        ) from exc


def _client_owned_vm_service(db: Session, service_id: int, user_id, permission: str):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.owner_user_id != int(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a VM service")
    require_client_permission(db, service, permission)
    return service


@router.get("/{service_id}/vm/backups")
async def client_list_vm_backups(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = _client_owned_vm_service(db, service_id, user_id, PermissionKey.VM_BACKUPS)
    try:
        items, jobs = await list_service_backups_and_jobs(db, service)
    except Exception as exc:
        raise map_backup_error(exc) from exc
    return {"backups": items, "jobs": jobs}


@router.post("/{service_id}/vm/backups")
async def client_create_vm_backup(
    service_id: int,
    body: BackupCreateBody,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = _client_owned_vm_service(db, service_id, user_id, PermissionKey.VM_BACKUPS)
    try:
        return await create_client_backup(
            db, service, notes=body.notes, mode=body.mode, wait=body.wait
        )
    except Exception as exc:
        raise map_backup_error(exc) from exc


@router.post("/{service_id}/vm/backups/delete")
async def client_delete_vm_backup(
    service_id: int,
    body: BackupMutateBody,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = _client_owned_vm_service(db, service_id, user_id, PermissionKey.VM_BACKUPS)
    try:
        await delete_client_backup(db, service, volid=body.volid, storage=body.storage)
    except Exception as exc:
        raise map_backup_error(exc) from exc
    return {"status": "ok"}


@router.post("/{service_id}/vm/backups/restore")
async def client_restore_vm_backup(
    service_id: int,
    body: BackupMutateBody,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = _client_owned_vm_service(db, service_id, user_id, PermissionKey.VM_BACKUPS)
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
        raise map_backup_error(exc) from exc
    if body.wait and service.vm:
        service.vm.guest_state = VMGuestState.RUNNING if body.start else VMGuestState.STOPPED
        service.vm.guest_last_error = None
        ServiceDAO.update(db, service)
    return result


@router.get("/{service_id}/vm/ssh-keys")
async def client_get_vm_ssh_keys(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return stored keys + reinstall template choices for the service owner."""
    from app.services.ssh_public_keys import ssh_key_fields_for_service
    from app.services.vm_ssh_keys_service import list_reinstall_templates_for_service

    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.owner_user_id != int(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a VM service")
    fields = ssh_key_fields_for_service(db, service)
    return {
        **fields,
        "vm_template_id": service.vm.vm_template_id if service.vm else None,
        "reinstall_templates": list_reinstall_templates_for_service(db, service),
    }


@router.put("/{service_id}/vm/ssh-keys")
async def client_put_vm_ssh_keys(
    service_id: int,
    body: VmSshKeysBody,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.vm_ssh_keys_service import VmSshKeysError, save_and_apply_ssh_public_keys

    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = _client_owned_vm_service(db, service_id, user_id, PermissionKey.VM_MANAGE_SSH_KEYS)
    try:
        return await save_and_apply_ssh_public_keys(db, service, body.ssh_public_keys)
    except VmSshKeysError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/{service_id}/vm/reinstall")
async def client_reinstall_vm(
    service_id: int,
    body: Optional[VmReinstallBody] = None,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Destroy guest (if any) and reprovision at the same reserved VMID."""
    from app.services.vm_reinstall_service import VmReinstallError, reinstall_vm_guest

    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    service = _client_owned_vm_service(db, service_id, user_id, PermissionKey.VM_REINSTALL)

    payload = body or VmReinstallBody()
    try:
        return await reinstall_vm_guest(
            db,
            service,
            vm_template_id=payload.vm_template_id,
            ssh_public_keys=payload.ssh_public_keys,
        )
    except VmReinstallError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/{service_id}/vm/vnc-popup")
async def vnc_popup_redirect(
    service_id: int,
    type: Optional[str] = None,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mint a one-time console launch ticket and redirect to ``/vnc?t=...``.

    Meant as the target of ``window.open(...)`` (a real browser popup,
    authenticated by the caller's own session cookie) so the console gets
    its own top-level clipboard/focus context, unlike the in-page modal.
    VM placement/power-state are validated by the redeem step on the
    ``/vnc`` page itself (same as the WHMCS popup flow), so this only needs
    the ownership + permission checks before minting the ticket. ``type``
    (``"vnc"``/``"serial"``) carries the console type the UI's picker
    chose, if any.
    """
    user_id = auth.get("user_id")
    if not user_id:
        return RedirectResponse(
            url=build_relative_error_url("User session required"), status_code=status.HTTP_302_FOUND
        )

    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.owner_user_id != int(user_id):
        return RedirectResponse(url=build_relative_error_url("Service not found"), status_code=status.HTTP_302_FOUND)
    if service.service_type != ServiceType.VM:
        return RedirectResponse(url=build_relative_error_url("Not a VM service"), status_code=status.HTTP_302_FOUND)

    try:
        require_client_permission(db, service, PermissionKey.VM_CONSOLE)
    except HTTPException as exc:
        return RedirectResponse(url=build_relative_error_url(str(exc.detail)), status_code=status.HTTP_302_FOUND)

    token = mint_launch_ticket(service.id, console_type=type)
    return RedirectResponse(url=build_relative_launch_url(token), status_code=status.HTTP_302_FOUND)
