"""
Billing API endpoints for external billing systems.

These endpoints are authenticated via API keys and provide standard operations
for billing systems to manage services (which link to servers).

The integration instance is automatically derived from the API key used for authentication.
All routes are generic and work for any integration type (WHMCS, custom, etc.).
"""
import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timezone
from app.core.database import get_db
from app.core.billing_auth import get_billing_integration
from app.models.billing_integration import BillingIntegration
from app.models.server import Server, BootMode
from app.models.service import Service, ServiceStatus
from app.schemas.billing import (
    BillingServiceCreate,
    BillingRegisterService,
    BillingServiceResponse,
    BillingServiceDetailResponse,
    PowerAction,
    SuspendAction,
    ServerUsage,
    ServiceActionRunScript,
    ServiceActionReinstallOS
)
from app.dao.server_dao import ServerDAO
from app.dao.server_group_dao import ServerGroupDAO
from app.dao.disk_dao import DiskDAO
from app.dao.network_port_dao import NetworkPortDAO
from app.plugins.registry import get_registry
from app.dao.location_dao import LocationDAO
from app.dao.external_user_dao import ExternalUserDAO
from app.dao.service_dao import ServiceDAO
from app.dao.installation_task_dao import InstallationTaskDAO
from app.dao.script_dao import ScriptDAO
from app.dao.boot_task_dao import BootTaskDAO
from app.models.disk import DiskType
from app.models.boot_task import BootTask, BootType, BootTaskStatus
from app.plugins.registry import get_registry
from app.plugins.base import PowerState
from app.services.os_template_service import get_template_service
from app.services.temp_os_service import get_temp_os_service
from app.services.download_token_service import get_download_token_service
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_success,
    log_server_activity_failure,
)
from app.models.server_activity import ServerActivityEventType
from app.models.service import ServiceStatus
from app.api.server_interaction import _get_base_url_for_pxe_ip
import logging
import os

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/server-by-ip")
async def get_server_by_ip(
    ip: str,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
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


@router.post("/register-service", response_model=BillingServiceResponse, status_code=status.HTTP_201_CREATED)
async def register_service(
    data: BillingRegisterService,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
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
        server = existing.server
        if server:
            log_server_activity_success(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.SERVICE,
                action="register",
                source="billing_api",
                message="Service registration request reused existing service",
                details={
                    "service_id": existing.id,
                    "external_service_id": existing.external_service_id,
                    "integration_id": integration.id,
                    "idempotent": True,
                },
            )
        return BillingServiceResponse(
            id=existing.id,
            name=existing.name,
            external_service_id=existing.external_service_id,
            server_id=existing.server_id,
            external_user_id=existing.external_user_id,
            status=existing.status.value,
            description=existing.description,
            config=existing.config,
            server_ip=server.server_ip if server else None,
            credentials=server.credentials if server else None,
            created_at=existing.created_at,
            updated_at=existing.updated_at,
        )

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

    # Find or create external user
    external_user = ExternalUserDAO.get_by_external_id(
        db, integration.id, data.external_user_id
    )
    if not external_user:
        external_user = ExternalUserDAO.create(
            db,
            integration_id=integration.id,
            external_user_id=data.external_user_id,
            external_username=data.external_username,
            external_email=data.external_email,
        )
        logger.info(f"Created external user (ID: {external_user.id}, external_user_id: {data.external_user_id})")

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
            "integration_id": integration.id,
        },
    )
    service = ServiceDAO.create(
        db,
        name=name,
        server_id=server.id,
        external_user_id=external_user.id,
        external_service_id=data.external_service_id,
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
    return BillingServiceResponse(
        id=service.id,
        name=service.name,
        external_service_id=service.external_service_id,
        server_id=service.server_id,
        external_user_id=service.external_user_id,
        status=service.status.value,
        description=service.description,
        config=service.config,
        server_ip=server.server_ip,
        credentials=server.credentials,
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


def _select_free_server_in_group(db: Session, group_id: int) -> Server:
    """
    Select a free server from a server group.

    "Free" means:
    - Server is enabled
    - Server belongs to the group
    - No associated services in non-terminated state
    """
    group = ServerGroupDAO.get_by_id(db, group_id)
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server group not found",
        )

    if not group.servers:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No servers in this server group",
        )

    candidates: list[Server] = []
    for server in group.servers:
        if not server.enabled:
            continue
        services = ServiceDAO.get_by_server(db, server.id)
        has_active = any(s.status != ServiceStatus.TERMINATED for s in services)
        if not has_active:
            candidates.append(server)

    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No free servers available in this group",
        )

    # Simple selection policy: smallest ID first for determinism
    candidates.sort(key=lambda s: s.id)
    return candidates[0]


def _determine_template_for_group(
    db: Session,
    group_id: int,
    explicit_template_id: str | None,
) -> str:
    """
    Determine which OS template to use for a server group.

    Rules:
    - If explicit_template_id is provided, verify it's permitted (when list is non-empty).
    - Otherwise, if exactly one permitted_os_templates entry exists, use that.
    - Else, require explicit template_id.
    """
    group = ServerGroupDAO.get_by_id(db, group_id)
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server group not found",
        )

    permitted = list(group.permitted_os_templates or [])

    if explicit_template_id:
        if permitted and explicit_template_id not in permitted:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Template '{explicit_template_id}' is not permitted for this server group",
            )
        return explicit_template_id

    # No explicit template; auto-select when there is exactly one permitted option
    if permitted and len(permitted) == 1:
        return permitted[0]

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="OS template must be specified for this server group",
    )


def _queue_template_install_for_service(
    db: Session,
    service: Service,
    template_id: str,
    template_parameters: dict | None,
) -> tuple[BootTask, object]:
    """
    Queue a template-based OS installation for the given service.server.

    This mirrors the template-handling logic in the server interaction API
    but is scoped for billing usage.
    """
    server = service.server
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

    # Template parameters (e.g. admin_password)
    template_parameters = template_parameters or {}
    for param_name, param_value in template_parameters.items():
        replacements[f"PARAM_{param_name.upper()}"] = str(param_value)

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
                allowed_files.append(rel_name)

    if template.disk_image and not template_image_files:
        disk_image_filename = template.disk_image.split("/")[-1]
        allowed_files.append(disk_image_filename)

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

    # Generate download token for template files (any file under template allowed; script fetches what it needs)
    download_token_service = get_download_token_service()
    download_token = download_token_service.generate_token(
        boot_task_id=boot_task.id,
        allowed_files=allowed_files if allowed_files else None,
        allowed_patterns=["*"],
        expires_in=900,
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

    # Ensure script_url is present in kernel params for debian-live
    if boot_task.temp_os_id == "debian-live" and boot_task.script_content:
        from urllib.parse import quote

        script_url = f"{base_url}/api/servers/interaction/scripts/{boot_task.id}"
        encoded_script_url = quote(script_url, safe=":/?=&")
        if "script_url=" not in boot_task.kernel_params:
            boot_task.kernel_params = f"{boot_task.kernel_params} script_url={encoded_script_url}"
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


@router.post("/services", response_model=BillingServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_service(
    service_data: BillingServiceCreate,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
):
    """
    Create a new service via billing API.

    External billing systems (WHMCS, etc.) use this to provision new services.
    There are two modes:

    1. Default mode (no server_group_id in service_config):
       - Create a brand new server record using the provided server_* fields.
       - No OS installation is queued automatically.

    2. Server group provisioning mode (service_config.server_group_id set):
       - Pick a "free" server from the specified server group (no active services).
       - Link it to the external user via a new Service in PENDING state.
       - Queue an OS installation using an OS template:
         * Use service_config.template_id when specified (and permitted),
         * Otherwise auto-select when the group has exactly one permitted_os_template.
       - Store OS credentials in server.credentials and expose them via the response.
    """
    logger.info(f"Billing API: Creating service '{service_data.name}' via integration '{integration.name}'")
    
    # Find or create external user
    external_user = ExternalUserDAO.get_by_external_id(
        db,
        integration.id,
        service_data.external_user_id
    )
    
    if not external_user:
        external_user = ExternalUserDAO.create(
            db,
            integration_id=integration.id,
            external_user_id=service_data.external_user_id,
            external_username=service_data.external_username,
            external_email=service_data.external_email
        )
        logger.info(f"Created external user (ID: {external_user.id}, external_user_id: {service_data.external_user_id})")
    
    # Validate plugin exists (plugins are loaded from disk, not database)
    registry = get_registry()
    plugin_class = registry.get_plugin(service_data.plugin_name)
    if not plugin_class:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{service_data.plugin_name}' not found"
        )
    
    # Validate location exists
    location = LocationDAO.get_by_id(db, service_data.location_id)
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found"
        )
    
    # Check if service with same name already exists
    existing_service = ServiceDAO.get_by_name(db, service_data.name)
    if existing_service:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Service with this name already exists"
        )
    
    # Decide whether to create a brand new server or reuse one from a server group
    service_config = service_data.service_config or {}
    server_group_id = service_config.get("server_group_id")

    if server_group_id:
        # --- Server group provisioning mode: pick an existing free server ---
        server = _select_free_server_in_group(db, int(server_group_id))

        # Link server to external user
        server.external_user_id = external_user.id
        ServerDAO.update(db, server)

        # Create service in PENDING state and attach original service_config
        service = ServiceDAO.create(
            db,
            name=service_data.name,
            server_id=server.id,
            external_user_id=external_user.id,
            external_service_id=service_data.external_service_id,
            status=ServiceStatus.PENDING,
            description=service_data.description,
            config=service_config,
        )
        db.refresh(service)
        log_server_activity_attempt(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.SERVICE,
            action="create",
            source="billing_api",
            message=f"Creating service '{service.name}'",
            details={
                "service_id": service.id,
                "integration_id": integration.id,
                "server_group_id": int(server_group_id),
            },
        )

        # Determine OS template to install
        explicit_template_id = service_config.get("template_id")
        template_params = service_config.get("template_parameters") or {}
        template_id = _determine_template_for_group(
            db, int(server_group_id), explicit_template_id
        )

        # Queue installation
        log_server_activity_attempt(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.INSTALL,
            action="queue_template_install",
            source="billing_api",
            message=f"Queueing template install '{template_id}'",
            details={
                "service_id": service.id,
                "template_id": template_id,
                "integration_id": integration.id,
            },
        )
        try:
            _, installation_task = _queue_template_install_for_service(
                db=db,
                service=service,
                template_id=template_id,
                template_parameters=template_params,
            )
        except Exception as exc:
            log_server_activity_failure(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.INSTALL,
                action="queue_template_install",
                source="billing_api",
                message=f"Failed to queue template install '{template_id}'",
                details={
                    "service_id": service.id,
                    "template_id": template_id,
                    "integration_id": integration.id,
                },
                error=exc,
            )
            log_server_activity_failure(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.SERVICE,
                action="create",
                source="billing_api",
                message=f"Service '{service.name}' provisioning failed",
                details={
                    "service_id": service.id,
                    "integration_id": integration.id,
                },
                error=exc,
            )
            raise

        log_server_activity_success(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.INSTALL,
            action="queue_template_install",
            source="billing_api",
            message=f"Queued template install '{template_id}'",
            details={
                "service_id": service.id,
                "template_id": template_id,
                "installation_task_id": installation_task.id,
                "boot_task_id": installation_task.boot_task_id,
            },
        )
        log_server_activity_success(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.SERVICE,
            action="create",
            source="billing_api",
            message=f"Created service '{service.name}'",
            details={
                "service_id": service.id,
                "integration_id": integration.id,
                "server_group_id": int(server_group_id),
            },
        )

        logger.info(
            "Billing API: Service '%s' (ID: %s) created on existing server %s via group %s",
            service.name,
            service.id,
            server.id,
            server_group_id,
        )
    else:
        # --- Default mode: create a brand new server record ---
        # Check if server with same name already exists
        existing_server = ServerDAO.get_by_name(db, service_data.server_name)
        if existing_server:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Server with this name already exists",
            )

        # Convert boot mode
        try:
            os_boot_mode = (
                BootMode(service_data.os_boot_mode.lower())
                if service_data.os_boot_mode
                else BootMode.UEFI
            )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid os_boot_mode: {service_data.os_boot_mode}. "
                    "Must be 'uefi' or 'bios'"
                ),
            )

        # Create server
        server = ServerDAO.create(
            db,
            name=service_data.server_name,
            server_ip=service_data.server_ip,
            description=service_data.description,
            cpu_count=service_data.cpu_count,
            cpu_model=service_data.cpu_model,
            ram_gb=service_data.ram_gb,
            port_speed_mbps=service_data.port_speed_mbps,
            location_id=service_data.location_id,
            plugin_name=service_data.plugin_name,
            plugin_config=service_data.plugin_config,
            enabled=True,
            boot_mode=os_boot_mode,  # For backward compatibility
            os_boot_mode=os_boot_mode,
            pxe_boot_mode=os_boot_mode,  # Default to same as OS boot mode
        )

        # Link server to external user
        server.external_user_id = external_user.id
        ServerDAO.update(db, server)

        # Create disks
        if service_data.disks:
            for disk_data in service_data.disks:
                try:
                    disk_type = DiskType(disk_data.get("type", "ssd").upper())
                except ValueError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Invalid disk type: {disk_data.get('type')}. "
                            "Must be 'ssd' or 'hdd'"
                        ),
                    )

                DiskDAO.create(
                    db,
                    server_id=server.id,
                    type=disk_type,
                    capacity_gb=disk_data.get("capacity_gb", 0),
                    description=disk_data.get("description"),
                    serial_number=disk_data.get("serial_number"),
                    is_os_disk=disk_data.get("is_os_disk", False),
                )

        # Create network ports
        if service_data.network_ports:
            for port_data in service_data.network_ports:
                NetworkPortDAO.create(
                    db,
                    server_id=server.id,
                    name=port_data.get("name", "eth0"),
                    mac_address=port_data.get("mac_address"),
                    speed_mbps=port_data.get("speed_mbps", 1000),
                    lag_group=port_data.get("lag_group"),
                    monitor_bandwidth=port_data.get("monitor_bandwidth", False),
                    pxe_boot=port_data.get("pxe_boot", False),
                    pxe_ip=port_data.get("pxe_ip"),
                    description=port_data.get("description"),
                )

        # Create service linking server to external user
        log_server_activity_attempt(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.SERVICE,
            action="create",
            source="billing_api",
            message=f"Creating service '{service_data.name}'",
            details={
                "external_service_id": service_data.external_service_id,
                "integration_id": integration.id,
            },
        )
        service = ServiceDAO.create(
            db,
            name=service_data.name,
            server_id=server.id,
            external_user_id=external_user.id,
            external_service_id=service_data.external_service_id,
            status=ServiceStatus.PENDING,
            description=service_data.description,
            config=service_data.service_config,
        )
        db.refresh(service)
        log_server_activity_success(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.SERVICE,
            action="create",
            source="billing_api",
            message=f"Created service '{service.name}'",
            details={
                "service_id": service.id,
                "external_service_id": service.external_service_id,
                "integration_id": integration.id,
            },
        )

        logger.info(
            "Billing API: Service '%s' (ID: %s) created successfully on new server %s",
            service.name,
            service.id,
            server.id,
        )

    return BillingServiceResponse(
        id=service.id,
        name=service.name,
        external_service_id=service.external_service_id,
        server_id=service.server_id,
        external_user_id=service.external_user_id,
        status=service.status.value,
        description=service.description,
        config=service.config,
        server_ip=server.server_ip,
        credentials=server.credentials,
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


@router.get("/services", response_model=List[BillingServiceResponse])
async def list_services(
    skip: int = 0,
    limit: int = 100,
    status_filter: Optional[str] = None,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
):
    """
    List services accessible via billing API.
    
    Only returns services for external users belonging to this integration.
    """
    # Get all external users for this integration
    external_users = ExternalUserDAO.get_by_integration(db, integration.id)
    external_user_ids = [eu.id for eu in external_users]
    
    if not external_user_ids:
        return []
    
    # Filter services by external user IDs
    query = db.query(Service).filter(Service.external_user_id.in_(external_user_ids))
    
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
    
    return [
        BillingServiceResponse(
            id=s.id,
            name=s.name,
            external_service_id=s.external_service_id,
            server_id=s.server_id,
            external_user_id=s.external_user_id,
            status=s.status.value,
            description=s.description,
            config=s.config,
            created_at=s.created_at,
            updated_at=s.updated_at
        )
        for s in services
    ]


@router.get("/services/{service_id}", response_model=BillingServiceDetailResponse)
async def get_service(
    service_id: int,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
):
    """Get service details by ID"""
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    # Verify service belongs to this integration
    if service.external_user.integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    server = service.server
    
    return BillingServiceDetailResponse(
        id=service.id,
        name=service.name,
        external_service_id=service.external_service_id,
        server_id=service.server_id,
        external_user_id=service.external_user_id,
        status=service.status.value,
        description=service.description,
        config=service.config,
        created_at=service.created_at,
        updated_at=service.updated_at,
        server={
            "id": server.id,
            "name": server.name,
            "server_ip": server.server_ip,
            "description": server.description,
            "cpu_count": server.cpu_count,
            "cpu_model": server.cpu_model,
            "ram_gb": server.ram_gb,
            "port_speed_mbps": server.port_speed_mbps,
            "enabled": server.enabled,
            "os_boot_mode": server.os_boot_mode.value
        },
        external_user={
            "id": service.external_user.id,
            "external_user_id": service.external_user.external_user_id,
            "external_username": service.external_user.external_username,
            "external_email": service.external_user.external_email
        }
    )


@router.delete("/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def terminate_service(
    service_id: int,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
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
            detail="Service not found"
        )
    
    # Verify service belongs to this integration
    if service.external_user.integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    # Update service status
    server = service.server
    log_server_activity_attempt(
        db,
        server_id=server.id,
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
        server_id=server.id,
        event_type=ServerActivityEventType.SERVICE,
        action="terminate",
        source="billing_api",
        message=f"Terminated service {service.id}",
        details={"service_id": service.id, "integration_id": integration.id},
    )
    
    logger.info(f"Billing API: Service {service_id} terminated successfully")
    return None


@router.post("/services/{service_id}/suspend", status_code=status.HTTP_200_OK)
async def suspend_service(
    service_id: int,
    action: SuspendAction,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
):
    """
    Suspend a service.
    
    Suspended services are blocked from power-on actions via service status.
    Server enabled/disabled is an administrative server-level flag and is not
    changed by service lifecycle actions.
    """
    logger.info(f"Billing API: Suspending service {service_id} via integration '{integration.name}'")
    
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    # Verify service belongs to this integration
    if service.external_user.integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    # Update service status
    server = service.server
    log_server_activity_attempt(
        db,
        server_id=server.id,
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
    service.status = ServiceStatus.SUSPENDED
    ServiceDAO.update(db, service)
    
    log_server_activity_success(
        db,
        server_id=server.id,
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


@router.post("/services/{service_id}/unsuspend", status_code=status.HTTP_200_OK)
async def unsuspend_service(
    service_id: int,
    action: SuspendAction,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
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
            detail="Service not found"
        )
    
    # Verify service belongs to this integration
    if service.external_user.integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    # Update service status
    server = service.server
    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.SERVICE,
        action="unsuspend",
        source="billing_api",
        message=f"Unsuspending service {service.id}",
        details={"service_id": service.id, "integration_id": integration.id},
    )
    service.status = ServiceStatus.ACTIVE
    ServiceDAO.update(db, service)
    
    log_server_activity_success(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.SERVICE,
        action="unsuspend",
        source="billing_api",
        message=f"Unsuspended service {service.id}",
        details={"service_id": service.id, "integration_id": integration.id},
    )
    
    logger.info(f"Billing API: Service {service_id} unsuspended")
    return {"status": "active", "message": "Service has been unsuspended"}


@router.post("/services/{service_id}/power", status_code=status.HTTP_200_OK)
async def power_control(
    service_id: int,
    power_action: PowerAction,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
):
    """
    Control server power state via service.
    
    Actions: on, off, reboot, reset
    """
    logger.info(f"Billing API: Power action '{power_action.action}' on service {service_id} via integration '{integration.name}'")
    
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    # Verify service belongs to this integration
    if service.external_user.integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    server = service.server

    action = power_action.action.lower()
    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.POWER,
        action=action,
        source="billing_api",
        message=f"Power action '{action}' requested",
        details={"service_id": service.id, "integration_id": integration.id},
    )

    try:
        # Prevent powering on suspended/terminated services.
        # Also block if server is administratively disabled.
        if action == "on":
            if service.status in (ServiceStatus.SUSPENDED, ServiceStatus.TERMINATED):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Cannot power on server for a {service.status.value} service",
                )
            if not server.enabled:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot power on an administratively disabled server",
                )

        registry = get_registry()
        plugin_instance = registry.get_plugin(server.plugin_name, server.plugin_config)
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
            server_id=server.id,
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
            server_id=server.id,
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
            server_id=server.id,
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
        server_id=server.id,
        event_type=ServerActivityEventType.POWER,
        action=action,
        source="billing_api",
        message=f"Power action '{action}' completed",
        details={"service_id": service.id, "integration_id": integration.id},
    )
    logger.info(f"Billing API: Power action '{action}' on service {service_id} completed successfully")
    return {"status": "success", "action": action, "message": f"Server power {action} command executed"}


@router.get("/services/{service_id}/status", status_code=status.HTTP_200_OK)
async def get_service_status(
    service_id: int,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
):
    """
    Get service status including server power state.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    # Verify service belongs to this integration
    if service.external_user.integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    server = service.server
    
    # Get power state from plugin
    power_state = PowerState.UNKNOWN
    try:
        registry = get_registry()
        plugin_instance = registry.get_plugin(server.plugin_name, server.plugin_config)
        if plugin_instance:
            power_state = await plugin_instance.get_power_state()
    except Exception as e:
        logger.warning(f"Billing API: Could not get power state for service {service_id}: {e}")
    
    return {
        "service_id": service.id,
        "service_name": service.name,
        "service_status": service.status.value,
        "server_id": server.id,
        "server_name": server.name,
        "server_enabled": server.enabled,
        "power_state": power_state.value,
        "status": "suspended"
        if service.status == ServiceStatus.SUSPENDED
        else (
            "on"
            if power_state == PowerState.ON
            else "off" if power_state == PowerState.OFF else "unknown"
        ),
        # Latest installation task summary (if any) so GUIs can show OS install status
        "installation": (
            lambda task: {
                "task_id": task.id,
                "status": task.status.value,
                "progress_percent": task.progress_percent,
                "os_name": task.os_name,
                "error_message": task.error_message,
                "created_at": task.created_at.isoformat()
                if task.created_at
                else None,
                "started_at": task.started_at.isoformat()
                if task.started_at
                else None,
                "completed_at": task.completed_at.isoformat()
                if task.completed_at
                else None,
            }
        )(
            (
                InstallationTaskDAO.get_active_by_server(db, server.id)
                or (
                    InstallationTaskDAO.get_by_server(db, server.id)[0]
                    if InstallationTaskDAO.get_by_server(db, server.id)
                    else None
                )
            )
        )
        if (
            InstallationTaskDAO.get_active_by_server(db, server.id)
            or InstallationTaskDAO.get_by_server(db, server.id)
        )
        else None,
    }


@router.get("/services/{service_id}/usage", response_model=ServerUsage)
async def get_service_usage(
    service_id: int,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
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
            detail="Service not found"
        )
    
    # Verify service belongs to this integration
    if service.external_user.integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    server = service.server
    
    # TODO: Implement actual usage collection from monitoring system
    # For now, return basic info
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


@router.post("/services/{service_id}/actions/run-script", status_code=status.HTTP_200_OK)
async def run_script_on_service(
    service_id: int,
    action: ServiceActionRunScript,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
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
            detail="Service not found"
        )
    
    # Verify service belongs to this integration
    if service.external_user.integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
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
    
    server = service.server
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
            script_url = f"{base_url}/api/servers/interaction/scripts/{boot_task.id}"
            from urllib.parse import quote
            encoded_script_url = quote(script_url, safe=':/?=&')
            if "script_url=" not in boot_task.kernel_params:
                updated_kernel_params = f"{boot_task.kernel_params} script_url={encoded_script_url}"
                boot_task.kernel_params = updated_kernel_params
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


@router.post("/services/{service_id}/actions/reinstall-os", status_code=status.HTTP_200_OK)
async def reinstall_os_on_service(
    service_id: int,
    action: ServiceActionReinstallOS,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
):
    """
    Reinstall OS on a service's server.
    
    Only OS templates marked as user_reinstallable can be used via this endpoint.
    """
    logger.info(f"Billing API: Reinstalling OS '{action.template_id}' on service {service_id} via integration '{integration.name}'")
    
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    # Verify service belongs to this integration
    if service.external_user.integration_id != integration.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found"
        )
    
    server = service.server
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

        # Verify template is user-reinstallable
        if not template.user_reinstallable:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"OS template '{action.template_id}' is not available for user reinstallation"
            )

        # Get template script
        script_path = template_service.get_template_script_path(action.template_id)
        if not script_path or not script_path.exists():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Template '{action.template_id}' missing installation script"
            )

        # Read script content
        with open(script_path, 'r') as f:
            script_content = f.read()

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

        # Add template parameters
        if action.template_parameters:
            for param_name, param_value in action.template_parameters.items():
                replacements[f"PARAM_{param_name.upper()}"] = str(param_value)

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

        # Generate download token for file access (any template file allowed; script fetches what it needs)
        allowed_files = [disk_image_filename] if disk_image_filename else []
        download_token_service = get_download_token_service()
        download_token = download_token_service.generate_token(
            boot_task_id=boot_task.id,
            allowed_files=allowed_files if allowed_files else None,
            allowed_patterns=["*"],
            expires_in=900,
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
            script_url = f"{base_url}/api/servers/interaction/scripts/{boot_task.id}"
            from urllib.parse import quote
            encoded_script_url = quote(script_url, safe=':/?=&')
            if "script_url=" not in boot_task.kernel_params:
                updated_kernel_params = f"{boot_task.kernel_params} script_url={encoded_script_url}"
                boot_task.kernel_params = updated_kernel_params
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


@router.get("/isos", response_model=List[dict])
async def list_isos_billing(
    integration: BillingIntegration = Depends(get_billing_integration),
):
    """
    List ISO files available for boot (read-only).
    Returns filenames for use in product configuration.
    """
    isos_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "isos",
    )
    if not os.path.exists(isos_dir):
        return []
    result = []
    for filename in os.listdir(isos_dir):
        file_path = os.path.join(isos_dir, filename)
        if os.path.isfile(file_path) and filename.lower().endswith(".iso"):
            result.append({
                "id": filename,
                "name": filename,
                "size_mb": round(os.path.getsize(file_path) / (1024 * 1024), 2),
            })
    return sorted(result, key=lambda x: x["name"])


@router.get("/temp-os", response_model=List[dict])
async def list_temp_os_billing(
    integration: BillingIntegration = Depends(get_billing_integration),
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


@router.get("/server-groups", response_model=List[dict])
async def list_server_groups_billing(
    skip: int = 0,
    limit: int = 100,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
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
        }
        for group in server_groups
    ]


@router.get("/scripts", response_model=List[dict])
async def list_available_scripts(
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
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


@router.get("/os-templates", response_model=List[dict])
async def list_available_os_templates(
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
):
    """
    List OS templates available for reinstallation via billing API.
    
    Only returns templates marked as user_reinstallable.
    """
    template_service = get_template_service()
    templates = template_service.get_all_templates()
    
    return [
        {
            "id": template.id,
            "name": template.name,
            "description": template.description,
            "os_type": template.os_type,
            "parameters": {
                name: {
                    "type": param.type,
                    "label": param.label,
                    "required": param.required,
                    "default": param.default,
                    "options": param.options,
                    "help": param.help
                }
                for name, param in template.parameters.items()
            }
        }
        for template in templates
        if template.user_reinstallable
    ]
