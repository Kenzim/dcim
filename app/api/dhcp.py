"""
DHCP Server Management API

Endpoints for managing the DHCP server subprocess.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from app.services.dhcp_service import get_dhcp_service, DHCPService
from app.services.dhcp_config_service import (
    get_dhcp_config_service,
    DHCPConfigService,
    DHCPConfig,
    DHCPInterfaceConfig
)
from app.core.auth import require_admin
from app.core.database import get_db
from app.services.dhcp_config_generator import generate_dhcpd_conf

router = APIRouter(prefix="/dhcp", tags=["dhcp"])


class DHCPConfigUpdate(BaseModel):
    """DHCP configuration update model"""
    enabled: Optional[bool] = None
    interfaces: Optional[List[DHCPInterfaceConfig]] = None
    hand_out_leases: Optional[bool] = None
    default_lease_time: Optional[int] = None
    max_lease_time: Optional[int] = None
    config_file_path: Optional[str] = None
    lease_file_path: Optional[str] = None


@router.post("/start", response_model=Dict[str, Any])
async def start_dhcp_server(
    auth: dict = Depends(require_admin),
    dhcp_service: DHCPService = Depends(get_dhcp_service)
):
    """
    Start the DHCP server.
    
    Returns:
        Status information about the DHCP server
    """
    result = await dhcp_service.start()
    
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("message", "Failed to start DHCP server")
        )
    
    return result


@router.post("/stop", response_model=Dict[str, Any])
async def stop_dhcp_server(
    auth: dict = Depends(require_admin),
    dhcp_service: DHCPService = Depends(get_dhcp_service)
):
    """
    Stop the DHCP server.
    
    Returns:
        Status information about the DHCP server
    """
    result = await dhcp_service.stop()
    
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("message", "Failed to stop DHCP server")
        )
    
    return result


@router.post("/restart", response_model=Dict[str, Any])
async def restart_dhcp_server(
    auth: dict = Depends(require_admin),
    dhcp_service: DHCPService = Depends(get_dhcp_service)
):
    """
    Restart the DHCP server.
    
    Returns:
        Status information about the DHCP server
    """
    result = await dhcp_service.restart()
    
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("message", "Failed to restart DHCP server")
        )
    
    return result


@router.post("/reload", response_model=Dict[str, Any])
async def reload_dhcp_server(
    auth: dict = Depends(require_admin),
    dhcp_service: DHCPService = Depends(get_dhcp_service)
):
    """
    Reload DHCP server configuration.
    
    Note: This will restart the server since dhcpd in foreground mode
    doesn't support graceful reload.
    
    Returns:
        Status information about the DHCP server
    """
    result = await dhcp_service.reload()
    
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("message", "Failed to reload DHCP server")
        )
    
    return result


@router.get("/status", response_model=Dict[str, Any])
async def get_dhcp_status(
    auth: dict = Depends(require_admin),
    dhcp_service: DHCPService = Depends(get_dhcp_service)
):
    """
    Get current DHCP server status.
    
    Returns:
        Current status, PID, and configuration information
    """
    return await dhcp_service.get_status()


@router.get("/config", response_model=DHCPConfig)
async def get_dhcp_config(
    auth: dict = Depends(require_admin),
    config_service: DHCPConfigService = Depends(get_dhcp_config_service)
):
    """
    Get DHCP server configuration.
    
    Returns:
        Current DHCP configuration
    """
    return config_service.get_config()


@router.post("/regenerate", response_model=Dict[str, Any])
async def regenerate_dhcp_config(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    config_service: DHCPConfigService = Depends(get_dhcp_config_service),
    dhcp_service: DHCPService = Depends(get_dhcp_service)
):
    """
    Manually regenerate DHCP configuration from current server settings.
    
    This will regenerate the dhcpd.conf file based on all servers with PXE boot ports
    and reload the DHCP server if it's currently running.
    
    Returns:
        Status information about the regeneration
    """
    try:
        config = config_service.get_config()
        
        # Regenerate dhcpd.conf file
        generate_dhcpd_conf(config, db)
        
        # Reload DHCP server if it's running
        status_info = await dhcp_service.get_status()
        if status_info.get("running"):
            await dhcp_service.reload()
        
        return {
            "success": True,
            "message": "DHCP configuration regenerated successfully",
            "status": "regenerated"
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to regenerate DHCP config: {str(e)}"
        )


@router.put("/config", response_model=DHCPConfig)
async def update_dhcp_config(
    config_data: DHCPConfigUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    config_service: DHCPConfigService = Depends(get_dhcp_config_service),
    dhcp_service: DHCPService = Depends(get_dhcp_service)
):
    """
    Update DHCP server configuration.
    
    This will regenerate the dhcpd.conf file and restart the DHCP server
    if it's currently running.
    
    Returns:
        Updated DHCP configuration
    """
    # Prepare update data (only include non-None values)
    update_data = {}
    if config_data.enabled is not None:
        update_data["enabled"] = config_data.enabled
    if config_data.interfaces is not None:
        update_data["interfaces"] = config_data.interfaces
    if config_data.hand_out_leases is not None:
        update_data["hand_out_leases"] = config_data.hand_out_leases
    if config_data.default_lease_time is not None:
        update_data["default_lease_time"] = config_data.default_lease_time
    if config_data.max_lease_time is not None:
        update_data["max_lease_time"] = config_data.max_lease_time
    if config_data.config_file_path is not None:
        update_data["config_file_path"] = config_data.config_file_path
    if config_data.lease_file_path is not None:
        update_data["lease_file_path"] = config_data.lease_file_path
    
    # Update configuration
    config = config_service.update_config(**update_data)
    
    # Regenerate dhcpd.conf file
    try:
        generate_dhcpd_conf(config, db)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate dhcpd.conf: {str(e)}"
        )
    
    # Restart DHCP server if it's running
    status_info = await dhcp_service.get_status()
    if status_info.get("running"):
        await dhcp_service.restart()
    
    return config
