from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, field_serializer
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import ServerDAO, PluginDAO, LocationDAO, DiskDAO, NetworkPortDAO
from app.models.server import Server
from app.models.plugin import Plugin
from app.models.disk import Disk, DiskType
from app.models.network_port import NetworkPort
from app.plugins.registry import get_registry
from app.plugins.base import PluginCategory, PowerState
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


def convert_disks_to_response(disks: List[Disk]) -> List[Dict[str, Any]]:
    """Convert Disk model objects to response format with lowercase type"""
    result = []
    for disk in disks:
        disk_dict = {
            "id": disk.id,
            "server_id": disk.server_id,
            "type": disk.type.value.lower() if hasattr(disk.type, 'value') else str(disk.type).lower(),
            "capacity_gb": disk.capacity_gb,
            "description": disk.description
        }
        result.append(disk_dict)
    return result


class DiskCreate(BaseModel):
    type: str  # "ssd" or "hdd"
    capacity_gb: int
    description: str | None = None


class DiskResponse(BaseModel):
    id: int
    server_id: int
    type: str
    capacity_gb: int
    description: str | None

    class Config:
        from_attributes = True
    
    @field_serializer('type')
    def serialize_type(self, value: Any) -> str:
        """Convert disk type to lowercase for API response"""
        if isinstance(value, str):
            return value.lower()
        # Handle enum values (DiskType enum)
        if hasattr(value, 'value'):
            return str(value.value).lower()
        # Handle DiskType enum directly
        if isinstance(value, DiskType):
            return value.value.lower()
        return str(value).lower()


class NetworkPortCreate(BaseModel):
    name: str
    mac_address: str | None = None
    speed_mbps: int
    lag_group: str | None = None
    monitor_bandwidth: bool = False
    pxe_boot: bool = False
    pxe_ip: str | None = None
    description: str | None = None


class NetworkPortResponse(BaseModel):
    id: int
    server_id: int
    name: str
    mac_address: str | None
    speed_mbps: int
    lag_group: str | None
    monitor_bandwidth: bool
    pxe_boot: bool
    pxe_ip: str | None
    description: str | None

    class Config:
        from_attributes = True


class ServerCreate(BaseModel):
    name: str
    server_ip: str
    description: str | None = None
    cpu_count: int = 1
    cpu_model: str | None = None
    ram_gb: int | None = None
    port_speed_mbps: int | None = None
    location_id: int
    plugin_id: int
    plugin_config: dict
    boot_mode: str = "uefi"  # "uefi" or "bios"
    disks: List[DiskCreate] = []
    network_ports: List[NetworkPortCreate] = []


class ServerUpdate(BaseModel):
    name: str | None = None
    server_ip: str | None = None
    description: str | None = None
    cpu_count: int | None = None
    cpu_model: str | None = None
    ram_gb: int | None = None
    port_speed_mbps: int | None = None
    location_id: int | None = None
    plugin_id: int | None = None
    plugin_config: dict | None = None
    enabled: bool | None = None
    disks: List[DiskCreate] | None = None
    network_ports: List[NetworkPortCreate] | None = None


class ServerResponse(BaseModel):
    id: int
    name: str
    server_ip: str
    description: str | None
    cpu_count: int
    cpu_model: str | None
    ram_gb: int | None
    port_speed_mbps: int | None
    location_id: int
    plugin_id: int
    plugin_config: dict
    enabled: bool
    boot_mode: str  # "uefi" or "bios"
    tested_capabilities: List[str] | None = None
    test_logs: str | None = None
    disks: List[DiskResponse] = []
    network_ports: List[NetworkPortResponse] = []
    plugin_categories: List[str] = []  # Categories supported by the plugin (e.g., 'power_control')

    class Config:
        from_attributes = True


class ServerTestRequest(BaseModel):
    plugin_id: int
    plugin_config: dict


async def _test_plugin_capabilities(plugin_instance, plugin_name: str) -> Dict[str, Any]:
    """
    Test all capabilities of a plugin instance.
    
    Returns:
        Dict with tested_capabilities list and test_logs string
    """
    # Get available capabilities
    available_capabilities = plugin_instance.get_capabilities()
    
    # Test each capability
    tested_capabilities = []
    test_logs = []
    
    def log(message: str):
        """Helper to log messages"""
        test_logs.append(message)
        logger.info(f"[Server Capability Test {plugin_name}] {message}")
    
    log(f"Starting capability test for plugin '{plugin_name}'")
    log(f"Available capabilities: {', '.join(available_capabilities)}")
    log("")
    
    # Test each capability
    for capability in available_capabilities:
        log(f"Testing capability: {capability}")
        try:
            if capability == "test_connection":
                result = await plugin_instance.test_connection()
                if result.get("success", False):
                    tested_capabilities.append(capability)
                    log(f"  ✓ {capability}: PASSED")
                else:
                    log(f"  ✗ {capability}: FAILED - {result.get('message', 'Unknown error')}")
            
            elif capability == "get_power_state":
                power_state = await plugin_instance.get_power_state()
                tested_capabilities.append(capability)
                log(f"  ✓ {capability}: PASSED (state: {power_state.value})")
            
            elif capability == "power_on":
                # Verify power control is available by checking if get_power_state worked
                # Don't actually power on - that would change server state
                if "get_power_state" in tested_capabilities:
                    tested_capabilities.append(capability)
                    log(f"  ✓ {capability}: AVAILABLE (power control verified via get_power_state)")
                else:
                    log(f"  ✗ {capability}: UNAVAILABLE (get_power_state test failed)")
            
            elif capability == "power_off":
                # Verify power control is available by checking if get_power_state worked
                # Don't actually power off - that would change server state
                if "get_power_state" in tested_capabilities:
                    tested_capabilities.append(capability)
                    log(f"  ✓ {capability}: AVAILABLE (power control verified via get_power_state)")
                else:
                    log(f"  ✗ {capability}: UNAVAILABLE (get_power_state test failed)")
            
            elif capability == "power_reset":
                # Verify power control is available by checking if get_power_state worked
                # Don't actually reset - that would change server state
                if "get_power_state" in tested_capabilities:
                    tested_capabilities.append(capability)
                    log(f"  ✓ {capability}: AVAILABLE (power control verified via get_power_state)")
                else:
                    log(f"  ✗ {capability}: UNAVAILABLE (get_power_state test failed)")
            
            elif capability == "list_users":
                users = await plugin_instance.list_users()
                tested_capabilities.append(capability)
                log(f"  ✓ {capability}: PASSED (found {len(users)} users)")
            
            elif capability == "create_user":
                # Don't actually create, just mark as available
                tested_capabilities.append(capability)
                log(f"  ✓ {capability}: AVAILABLE (not tested - would create user)")
            
            elif capability == "delete_user":
                # Don't actually delete, just mark as available
                tested_capabilities.append(capability)
                log(f"  ✓ {capability}: AVAILABLE (not tested - would delete user)")
            
            elif capability == "update_user_password":
                # Don't actually update, just mark as available
                tested_capabilities.append(capability)
                log(f"  ✓ {capability}: AVAILABLE (not tested - would update password)")
            
            elif capability == "get_boot_order":
                boot_order = await plugin_instance.get_boot_order()
                tested_capabilities.append(capability)
                log(f"  ✓ {capability}: PASSED (boot order: {boot_order})")
            
            elif capability == "set_boot_order":
                # Don't actually set, just mark as available
                tested_capabilities.append(capability)
                log(f"  ✓ {capability}: AVAILABLE (not tested - would set boot order)")
            
            elif capability == "set_next_boot_device":
                # Don't actually set, just mark as available
                tested_capabilities.append(capability)
                log(f"  ✓ {capability}: AVAILABLE (not tested - would set next boot device)")
            
            else:
                log(f"  ? {capability}: UNKNOWN CAPABILITY")
        
        except NotImplementedError as e:
            log(f"  ✗ {capability}: NOT IMPLEMENTED - {str(e)}")
        except Exception as e:
            log(f"  ✗ {capability}: FAILED - {str(e)}")
        
        log("")
    
    log(f"Test completed. {len(tested_capabilities)}/{len(available_capabilities)} capabilities available.")
    
    return {
        "tested_capabilities": tested_capabilities,
        "test_logs": "\n".join(test_logs)
    }


@router.post("/test", response_model=dict)
async def test_server_connection(
    test_data: ServerTestRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Test server connection using plugin's test_connection method"""
    # Validate plugin exists
    plugin = PluginDAO.get_by_id(db, test_data.plugin_id)
    if not plugin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plugin not found"
        )
    
    # Get plugin instance from registry
    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(plugin.name, test_data.plugin_config)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{plugin.name}' not found in registry"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to instantiate plugin: {str(e)}"
        )
    
    # Call test_connection
    try:
        result = await plugin_instance.test_connection()
        return result
    except Exception as e:
        return {
            "success": False,
            "message": f"Connection test failed: {str(e)}",
            "details": {"error": str(e)}
        }


@router.post("/test-capabilities", response_model=dict)
async def test_plugin_capabilities(
    test_data: ServerTestRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Test plugin capabilities without requiring a server.
    
    This endpoint is used during server creation/editing to test
    capabilities before saving the server.
    """
    # Validate plugin exists
    plugin = PluginDAO.get_by_id(db, test_data.plugin_id)
    if not plugin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plugin not found"
        )
    
    # Get plugin instance from registry
    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(plugin.name, test_data.plugin_config)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{plugin.name}' not found in registry"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to instantiate plugin: {str(e)}"
        )
    
    # Test capabilities
    try:
        test_results = await _test_plugin_capabilities(plugin_instance, plugin.name)
        return {
            "success": True,
            "available_capabilities": plugin.available_capabilities or [],
            "tested_capabilities": test_results["tested_capabilities"],
            "test_logs": test_results["test_logs"],
            "summary": {
                "total": len(plugin.available_capabilities or []),
                "tested": len(test_results["tested_capabilities"]),
                "failed": len(plugin.available_capabilities or []) - len(test_results["tested_capabilities"])
            }
        }
    except Exception as e:
        logger.error(f"Failed to test capabilities: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to test capabilities: {str(e)}"
        )


@router.post("/{server_id}/test-capabilities", response_model=dict)
async def test_server_capabilities(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Test all capabilities for a specific server.
    
    This endpoint tests each capability the plugin claims to support
    and stores the results in the server record.
    """
    # Get server
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    # Get plugin
    plugin = PluginDAO.get_by_id(db, server.plugin_id)
    if not plugin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plugin not found"
        )
    
    # Get plugin instance
    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(plugin.name, server.plugin_config)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to instantiate plugin: {str(e)}"
        )
    
    # Test capabilities
    try:
        test_results = await _test_plugin_capabilities(plugin_instance, plugin.name)
        
        # Update server with test results
        server.tested_capabilities = test_results["tested_capabilities"]
        server.test_logs = test_results["test_logs"]
        db.commit()
        db.refresh(server)
        
        return {
            "success": True,
            "server_id": server_id,
            "server_name": server.name,
            "available_capabilities": plugin.available_capabilities or [],
            "tested_capabilities": test_results["tested_capabilities"],
            "test_logs": test_results["test_logs"],
            "summary": {
                "total": len(plugin.available_capabilities or []),
                "tested": len(test_results["tested_capabilities"]),
                "failed": len(plugin.available_capabilities or []) - len(test_results["tested_capabilities"])
            }
        }
    except Exception as e:
        logger.error(f"Failed to test capabilities for server {server_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to test capabilities: {str(e)}"
        )


@router.get("/", response_model=List[ServerResponse])
async def list_servers(
    skip: int = 0,
    limit: int = 100,
    enabled_only: bool = False,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all servers"""
    servers = ServerDAO.get_all(db, skip=skip, limit=limit, enabled_only=enabled_only)
    result = []
    for server in servers:
        disks = DiskDAO.get_by_server(db, server.id)
        # Get plugin categories for power control support
        # Use joinedload to eagerly load categories relationship
        plugin = db.query(Plugin).options(joinedload(Plugin.categories)).filter(Plugin.id == server.plugin_id).first()
        plugin_categories = []
        if plugin and plugin.categories:
            plugin_categories = [cat.name for cat in plugin.categories]
        
        network_ports = NetworkPortDAO.get_by_server(db, server.id)
        result.append({
            **{k: v.value if hasattr(v, 'value') else v for k, v in server.__dict__.items()},
            "disks": convert_disks_to_response(disks),
            "network_ports": network_ports,
            "plugin_categories": plugin_categories
        })
    return result


@router.post("/", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server(
    server_data: ServerCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Create a new server"""
    try:
        logger.info(f"Creating server: name={server_data.name}, plugin_id={server_data.plugin_id}, location_id={server_data.location_id}")
        logger.debug(f"Server data: disks={len(server_data.disks) if server_data.disks else 0}, network_ports={len(server_data.network_ports) if server_data.network_ports else 0}")
    except Exception as e:
        logger.warning(f"Error logging server creation request: {e}")
    
    try:
        # Validate plugin exists
        plugin = PluginDAO.get_by_id(db, server_data.plugin_id)
        if not plugin:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Plugin not found"
            )
        
        # Validate location (required)
        location = LocationDAO.get_by_id(db, server_data.location_id)
        if not location:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Location not found"
            )
        
        # Check if server with same name already exists
        existing = ServerDAO.get_by_name(db, server_data.name)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Server with this name already exists"
            )
        
        # Convert boot_mode string to enum
        try:
            from app.models.server import BootMode
            boot_mode = BootMode(server_data.boot_mode.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid boot_mode: {server_data.boot_mode}. Must be 'uefi' or 'bios'"
            )
        
        # Create server
        server = ServerDAO.create(
            db,
            name=server_data.name,
            server_ip=server_data.server_ip,
            description=server_data.description,
            cpu_count=server_data.cpu_count,
            cpu_model=server_data.cpu_model,
            ram_gb=server_data.ram_gb,
            port_speed_mbps=server_data.port_speed_mbps,
            location_id=server_data.location_id,
            plugin_id=server_data.plugin_id,
            plugin_config=server_data.plugin_config,
            boot_mode=boot_mode
        )
        
        # Create disks
        if server_data.disks:
            for disk_data in server_data.disks:
                try:
                    # Convert to uppercase to match enum values (SSD, HDD)
                    disk_type = DiskType(disk_data.type.upper())
                except ValueError as e:
                    logger.error(f"Invalid disk type: {disk_data.type}. Error: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid disk type: {disk_data.type}. Must be 'ssd' or 'hdd'"
                    )
                
                DiskDAO.create(
                    db,
                    server_id=server.id,
                    type=disk_type,
                    capacity_gb=disk_data.capacity_gb,
                    description=disk_data.description
                )
        
        # Create network ports
        if server_data.network_ports:
            for port_data in server_data.network_ports:
                NetworkPortDAO.create(
                    db,
                    server_id=server.id,
                    name=port_data.name,
                    mac_address=port_data.mac_address,
                    speed_mbps=port_data.speed_mbps,
                    lag_group=port_data.lag_group,
                    monitor_bandwidth=port_data.monitor_bandwidth,
                    pxe_boot=port_data.pxe_boot,
                    pxe_ip=port_data.pxe_ip,
                    description=port_data.description
                )
        
        # Refresh to get disks and network ports
        db.refresh(server)
        disks = DiskDAO.get_by_server(db, server.id)
        network_ports = NetworkPortDAO.get_by_server(db, server.id)
        
        # Automatically test capabilities after creating server
        try:
            registry = get_registry()
            plugin_instance = registry.get_plugin(plugin.name, server.plugin_config)
            test_results = await _test_plugin_capabilities(plugin_instance, plugin.name)
            server.tested_capabilities = test_results["tested_capabilities"]
            server.test_logs = test_results["test_logs"]
            db.commit()
            db.refresh(server)
        except Exception as e:
            logger.warning(f"Failed to test capabilities for new server {server.id}: {e}")
            # Don't fail the creation if capability test fails
        
        return {
            **{k: v.value if hasattr(v, 'value') else v for k, v in server.__dict__.items()},
            "disks": convert_disks_to_response(disks),
            "network_ports": network_ports
        }
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        logger.error(f"Unexpected error creating server: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating server: {str(e)}"
        )


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get a server by ID"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    disks = DiskDAO.get_by_server(db, server.id)
    network_ports = NetworkPortDAO.get_by_server(db, server.id)
    
    # Get plugin categories for power control support
    plugin = db.query(Plugin).options(joinedload(Plugin.categories)).filter(Plugin.id == server.plugin_id).first()
    plugin_categories = []
    if plugin and plugin.categories:
        plugin_categories = [cat.name for cat in plugin.categories]
    
    return {
        **{k: v.value if hasattr(v, 'value') else v for k, v in server.__dict__.items()},
        "disks": convert_disks_to_response(disks),
        "network_ports": network_ports,
        "plugin_categories": plugin_categories
    }


@router.put("/{server_id}", response_model=ServerResponse)
async def update_server(
    server_id: int,
    server_data: ServerUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Update a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    # Update fields if provided
    if server_data.name is not None:
        # Check if name is being changed and if new name already exists
        if server_data.name != server.name:
            existing = ServerDAO.get_by_name(db, server_data.name)
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Server with this name already exists"
                )
        server.name = server_data.name
    
    if server_data.server_ip is not None:
        server.server_ip = server_data.server_ip
    if server_data.description is not None:
        server.description = server_data.description
    if server_data.cpu_count is not None:
        server.cpu_count = server_data.cpu_count
    if server_data.cpu_model is not None:
        server.cpu_model = server_data.cpu_model
    if server_data.ram_gb is not None:
        server.ram_gb = server_data.ram_gb
    if server_data.port_speed_mbps is not None:
        server.port_speed_mbps = server_data.port_speed_mbps
    if server_data.location_id is not None:
        location = LocationDAO.get_by_id(db, server_data.location_id)
        if not location:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Location not found"
            )
        server.location_id = server_data.location_id
    if server_data.plugin_id is not None:
        plugin = PluginDAO.get_by_id(db, server_data.plugin_id)
        if not plugin:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Plugin not found"
            )
        server.plugin_id = server_data.plugin_id
    if server_data.plugin_config is not None:
        server.plugin_config = server_data.plugin_config
    if server_data.enabled is not None:
        server.enabled = server_data.enabled
    if server_data.boot_mode is not None:
        try:
            from app.models.server import BootMode
            server.boot_mode = BootMode(server_data.boot_mode.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid boot_mode: {server_data.boot_mode}. Must be 'uefi' or 'bios'"
            )
    
    # Update disks if provided
    if server_data.disks is not None:
        # Delete existing disks
        DiskDAO.delete_by_server(db, server.id)
        # Create new disks
        for disk_data in server_data.disks:
            try:
                # Convert to uppercase to match enum values (SSD, HDD)
                disk_type = DiskType(disk_data.type.upper())
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid disk type: {disk_data.type}. Must be 'ssd' or 'hdd'"
                )
            
            DiskDAO.create(
                db,
                server_id=server.id,
                type=disk_type,
                capacity_gb=disk_data.capacity_gb,
                description=disk_data.description
            )
    
    # Update network ports if provided
    if server_data.network_ports is not None:
        # Delete existing network ports
        NetworkPortDAO.delete_by_server(db, server.id)
        # Create new network ports
        for port_data in server_data.network_ports:
            NetworkPortDAO.create(
                db,
                server_id=server.id,
                name=port_data.name,
                mac_address=port_data.mac_address,
                speed_mbps=port_data.speed_mbps,
                lag_group=port_data.lag_group,
                monitor_bandwidth=port_data.monitor_bandwidth,
                pxe_boot=port_data.pxe_boot,
                pxe_ip=port_data.pxe_ip,
                description=port_data.description
            )
    
    ServerDAO.update(db, server)
    
    # Refresh to get disks and network ports
    disks = DiskDAO.get_by_server(db, server.id)
    network_ports = NetworkPortDAO.get_by_server(db, server.id)
    
    # Automatically test capabilities after updating server (if plugin or config changed)
    if server_data.plugin_id is not None or server_data.plugin_config is not None:
        try:
            plugin = PluginDAO.get_by_id(db, server.plugin_id)
            if plugin:
                registry = get_registry()
                plugin_instance = registry.get_plugin(plugin.name, server.plugin_config)
                test_results = await _test_plugin_capabilities(plugin_instance, plugin.name)
                server.tested_capabilities = test_results["tested_capabilities"]
                server.test_logs = test_results["test_logs"]
                db.commit()
                db.refresh(server)
        except Exception as e:
            logger.warning(f"Failed to test capabilities for updated server {server.id}: {e}")
            # Don't fail the update if capability test fails
    
    return {
        **{k: v.value if hasattr(v, 'value') else v for k, v in server.__dict__.items()},
        "disks": convert_disks_to_response(disks),
        "network_ports": network_ports
    }


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete a server"""
    success = ServerDAO.delete(db, server_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )


# ========== Power Control Endpoints ==========

@router.get("/{server_id}/power-state", response_model=dict)
async def get_server_power_state(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get current power state of a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    # Get plugin and verify it supports power control
    plugin = PluginDAO.get_by_id(db, server.plugin_id)
    if not plugin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plugin not found"
        )
    
    plugin_categories = [cat.name for cat in plugin.categories]
    if PluginCategory.POWER_CONTROL.value not in plugin_categories:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plugin '{plugin.name}' does not support power control"
        )
    
    # Get plugin instance and call get_power_state
    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(plugin.name, server.plugin_config)
        power_state = await plugin_instance.get_power_state()
        return {
            "power_state": power_state.value,
            "server_id": server_id,
            "server_name": server.name
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get power state: {str(e)}"
        )


@router.post("/{server_id}/power-on", response_model=dict)
async def power_on_server(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Power on a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    # Get plugin and verify it supports power control
    plugin = PluginDAO.get_by_id(db, server.plugin_id)
    if not plugin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plugin not found"
        )
    
    plugin_categories = [cat.name for cat in plugin.categories]
    if PluginCategory.POWER_CONTROL.value not in plugin_categories:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plugin '{plugin.name}' does not support power control"
        )
    
    # Get plugin instance and call power_on
    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(plugin.name, server.plugin_config)
        success = await plugin_instance.power_on()
        return {
            "success": success,
            "message": "Power on command sent successfully" if success else "Power on command failed",
            "server_id": server_id,
            "server_name": server.name
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to power on server: {str(e)}"
        )


@router.post("/{server_id}/power-off", response_model=dict)
async def power_off_server(
    server_id: int,
    force: bool = False,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Power off a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    # Get plugin and verify it supports power control
    plugin = PluginDAO.get_by_id(db, server.plugin_id)
    if not plugin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plugin not found"
        )
    
    plugin_categories = [cat.name for cat in plugin.categories]
    if PluginCategory.POWER_CONTROL.value not in plugin_categories:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plugin '{plugin.name}' does not support power control"
        )
    
    # Get plugin instance and call power_off
    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(plugin.name, server.plugin_config)
        success = await plugin_instance.power_off(force=force)
        return {
            "success": success,
            "message": "Power off command sent successfully" if success else "Power off command failed",
            "server_id": server_id,
            "server_name": server.name,
            "force": force
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to power off server: {str(e)}"
        )


@router.post("/{server_id}/power-reset", response_model=dict)
async def power_reset_server(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Reset/reboot a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    # Get plugin and verify it supports power control
    plugin = PluginDAO.get_by_id(db, server.plugin_id)
    if not plugin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plugin not found"
        )
    
    plugin_categories = [cat.name for cat in plugin.categories]
    if PluginCategory.POWER_CONTROL.value not in plugin_categories:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plugin '{plugin.name}' does not support power control"
        )
    
    # Get plugin instance and call power_reset
    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(plugin.name, server.plugin_config)
        success = await plugin_instance.power_reset()
        return {
            "success": success,
            "message": "Reset command sent successfully" if success else "Reset command failed",
            "server_id": server_id,
            "server_name": server.name
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reset server: {str(e)}"
        )

