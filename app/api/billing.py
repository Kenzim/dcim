"""
Billing API endpoints for external billing systems.

These endpoints are authenticated via API keys and provide standard operations
for billing systems to manage services (which link to servers).

The integration instance is automatically derived from the API key used for authentication.
All routes are generic and work for any integration type (WHMCS, custom, etc.).
"""
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
from app.dao.script_dao import ScriptDAO
from app.dao.boot_task_dao import BootTaskDAO
from app.models.disk import DiskType
from app.models.boot_task import BootTask, BootType, BootTaskStatus
from app.plugins.registry import get_registry
from app.plugins.base import PowerState
from app.services.os_template_service import get_template_service
from app.services.temp_os_service import get_temp_os_service
from app.services.download_token_service import get_download_token_service
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
        return BillingServiceResponse(
            id=existing.id,
            name=existing.name,
            external_service_id=existing.external_service_id,
            server_id=existing.server_id,
            external_user_id=existing.external_user_id,
            status=existing.status.value,
            description=existing.description,
            config=existing.config,
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
    return BillingServiceResponse(
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
    )


@router.post("/services", response_model=BillingServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_service(
    service_data: BillingServiceCreate,
    integration: BillingIntegration = Depends(get_billing_integration),
    db: Session = Depends(get_db)
):
    """
    Create a new service via billing API.
    
    External billing systems (WHMCS, etc.) use this to provision new services.
    This will create/find the external user, create a server, and link them via a service.
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
    
    # Check if server with same name already exists
    existing_server = ServerDAO.get_by_name(db, service_data.server_name)
    if existing_server:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server with this name already exists"
        )
    
    # Convert boot mode
    try:
        os_boot_mode = BootMode(service_data.os_boot_mode.lower()) if service_data.os_boot_mode else BootMode.UEFI
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid os_boot_mode: {service_data.os_boot_mode}. Must be 'uefi' or 'bios'"
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
        pxe_boot_mode=os_boot_mode  # Default to same as OS boot mode
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
                    detail=f"Invalid disk type: {disk_data.get('type')}. Must be 'ssd' or 'hdd'"
                )
            
            DiskDAO.create(
                db,
                server_id=server.id,
                type=disk_type,
                capacity_gb=disk_data.get("capacity_gb", 0),
                description=disk_data.get("description"),
                serial_number=disk_data.get("serial_number"),
                is_os_disk=disk_data.get("is_os_disk", False)
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
                description=port_data.get("description")
            )
    
    # Create service linking server to external user
    service = ServiceDAO.create(
        db,
        name=service_data.name,
        server_id=server.id,
        external_user_id=external_user.id,
        external_service_id=service_data.external_service_id,
        status=ServiceStatus.PENDING,
        description=service_data.description,
        config=service_data.service_config
    )
    
    db.refresh(service)
    logger.info(f"Billing API: Service '{service.name}' (ID: {service.id}) created successfully")
    
    return BillingServiceResponse(
        id=service.id,
        name=service.name,
        external_service_id=service.external_service_id,
        server_id=service.server_id,
        external_user_id=service.external_user_id,
        status=service.status.value,
        description=service.description,
        config=service.config,
        created_at=service.created_at,
        updated_at=service.updated_at
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
    
    This marks the service as terminated and disables the associated server.
    The server and service records are not deleted (for audit purposes).
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
    service.status = ServiceStatus.TERMINATED
    service.terminated_at = datetime.now(timezone.utc)
    ServiceDAO.update(db, service)
    
    # Disable server
    server = service.server
    server.enabled = False
    ServerDAO.update(db, server)
    
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
    
    Suspended services have their servers disabled.
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
    service.status = ServiceStatus.SUSPENDED
    ServiceDAO.update(db, service)
    
    # Disable server
    server = service.server
    server.enabled = False
    ServerDAO.update(db, server)
    
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
    
    Re-enables a suspended service and its server.
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
    service.status = ServiceStatus.ACTIVE
    ServiceDAO.update(db, service)
    
    # Enable server
    server = service.server
    server.enabled = True
    ServerDAO.update(db, server)
    
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
    
    # Get plugin instance
    registry = get_registry()
    plugin_instance = registry.get_plugin(server.plugin_name, server.plugin_config)
    
    if not plugin_instance:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize plugin"
        )
    
    # Execute power action
    action = power_action.action.lower()
    success = False
    
    try:
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
    except NotImplementedError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server plugin does not support power control"
        )
    except Exception as e:
        logger.error(f"Billing API: Power action failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Power action failed: {str(e)}"
        )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Power action command failed"
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
        "status": "suspended" if service.status == ServiceStatus.SUSPENDED else ("on" if power_state == PowerState.ON else "off" if power_state == PowerState.OFF else "unknown")
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
            detail="debian-live temporary OS not found"
        )
    
    # Get URLs
    base_url = "http://192.168.12.74:8000"
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
    
    server = service.server
    
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
    base_url = "http://192.168.12.74:8000"
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
    
    # Generate download token for file access
    allowed_files = []
    if disk_image_filename:
        allowed_files.append(disk_image_filename)
    
    download_token_service = get_download_token_service()
    download_token = download_token_service.generate_token(
        boot_task_id=boot_task.id,
        allowed_files=allowed_files if allowed_files else None
    )
    
    # Inject token into script and update URLs
    if download_token:
        replacements["DOWNLOAD_TOKEN"] = download_token
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
