"""
TFTP Server Management API

Endpoints for managing the TFTP server subprocess.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional
from pydantic import BaseModel
from app.services.tftp_service import get_tftp_service, TFTPService
from app.services.tftp_config_service import (
    get_tftp_config_service,
    TFTPConfigService,
    TFTPConfig
)
from app.core.auth import require_admin
from app.core.database import get_db

router = APIRouter(prefix="/tftp", tags=["tftp"])


def get_tftp_service_with_db(db: Session = Depends(get_db)):
    """Provide TFTPService with DB so it can load config from database."""
    return get_tftp_service(db)


class TFTPConfigUpdate(BaseModel):
    """TFTP configuration update model"""
    enabled: Optional[bool] = None
    root_directory: Optional[str] = None
    bind_address: Optional[str] = None
    bind_port: Optional[int] = None
    allow_create: Optional[bool] = None
    verbose: Optional[bool] = None
    ipv4_only: Optional[bool] = None


@router.post("/start", response_model=Dict[str, Any])
async def start_tftp_server(
    auth: dict = Depends(require_admin),
    tftp_service: TFTPService = Depends(get_tftp_service_with_db)
):
    """
    Start the TFTP server.
    
    Returns:
        Status information about the TFTP server
    """
    result = await tftp_service.start()
    
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("message", "Failed to start TFTP server")
        )
    
    return result


@router.post("/stop", response_model=Dict[str, Any])
async def stop_tftp_server(
    auth: dict = Depends(require_admin),
    tftp_service: TFTPService = Depends(get_tftp_service_with_db)
):
    """
    Stop the TFTP server.
    
    Returns:
        Status information about the TFTP server
    """
    result = await tftp_service.stop()
    
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("message", "Failed to stop TFTP server")
        )
    
    return result


@router.post("/restart", response_model=Dict[str, Any])
async def restart_tftp_server(
    auth: dict = Depends(require_admin),
    tftp_service: TFTPService = Depends(get_tftp_service_with_db)
):
    """
    Restart the TFTP server.
    
    Returns:
        Status information about the TFTP server
    """
    result = await tftp_service.restart()
    
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("message", "Failed to restart TFTP server")
        )
    
    return result


@router.post("/reload", response_model=Dict[str, Any])
async def reload_tftp_server(
    auth: dict = Depends(require_admin),
    tftp_service: TFTPService = Depends(get_tftp_service_with_db)
):
    """
    Reload TFTP server configuration.
    
    Note: This will restart the server since TFTP doesn't support graceful reload.
    
    Returns:
        Status information about the TFTP server
    """
    result = await tftp_service.reload()
    
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("message", "Failed to reload TFTP server")
        )
    
    return result


@router.get("/status", response_model=Dict[str, Any])
async def get_tftp_status(
    auth: dict = Depends(require_admin),
    tftp_service: TFTPService = Depends(get_tftp_service_with_db)
):
    """
    Get current TFTP server status.
    
    Returns:
        Current status, PID, and configuration information
    """
    return await tftp_service.get_status()


@router.get("/config", response_model=TFTPConfig)
async def get_tftp_config(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    config_service: TFTPConfigService = Depends(get_tftp_config_service),
):
    """
    Get TFTP server configuration.

    Returns:
        Current TFTP configuration
    """
    return config_service.get_config(db)


@router.put("/config", response_model=TFTPConfig)
async def update_tftp_config(
    config_data: TFTPConfigUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    config_service: TFTPConfigService = Depends(get_tftp_config_service),
    tftp_service: TFTPService = Depends(get_tftp_service_with_db),
):
    """
    Update TFTP server configuration.
    
    This will restart the TFTP server if it's currently running.
    
    Returns:
        Updated TFTP configuration
    """
    # Prepare update data (only include non-None values)
    update_data = {}
    if config_data.enabled is not None:
        update_data["enabled"] = config_data.enabled
    if config_data.root_directory is not None:
        update_data["root_directory"] = config_data.root_directory
    if config_data.bind_address is not None:
        update_data["bind_address"] = config_data.bind_address
    if config_data.bind_port is not None:
        update_data["bind_port"] = config_data.bind_port
    if config_data.allow_create is not None:
        update_data["allow_create"] = config_data.allow_create
    if config_data.verbose is not None:
        update_data["verbose"] = config_data.verbose
    if config_data.ipv4_only is not None:
        update_data["ipv4_only"] = config_data.ipv4_only
    
    # Update configuration
    config = config_service.update_config(db, **update_data)

    # Start or restart TFTP server so it uses the new config
    status_info = await tftp_service.get_status()
    if status_info.get("running"):
        await tftp_service.restart()
    elif config.enabled:
        await tftp_service.start()

    return config
