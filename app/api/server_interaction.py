"""
API endpoints for server interactions (PXE boot, network config, password updates, etc.)

This module handles all server-to-API communication, including:
- PXE boot operations
- Network configuration retrieval
- Password updates
- Other server management operations
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from typing import Optional
from app.core.database import get_db
from app.dao import NetworkPortDAO, ServerDAO
from app.models.network_port import NetworkPort
import logging
import os

logger = logging.getLogger(__name__)

router = APIRouter()


def normalize_mac_address(mac: str) -> str:
    """
    Normalize MAC address to uppercase with colons.
    
    Examples:
        "00:0e:1e:6f:16:b0" -> "00:0E:1E:6F:16:B0"
        "00-0e-1e-6f-16-b0" -> "00:0E:1E:6F:16:B0"
        "000e1e6f16b0" -> "00:0E:1E:6F:16:B0"
    
    Raises:
        ValueError: If mac is None or empty
    """
    if not mac:
        raise ValueError("MAC address cannot be None or empty")
    
    # Remove separators and convert to uppercase
    mac_clean = mac.replace(":", "").replace("-", "").replace(".", "").upper()
    
    # Add colons every 2 characters
    if len(mac_clean) == 12:
        return ":".join(mac_clean[i:i+2] for i in range(0, 12, 2))
    
    return mac.upper()


@router.get("/pxe", response_class=PlainTextResponse)
async def get_pxe_boot_file(
    mac: Optional[str] = Query(None, description="MAC address of the network port requesting PXE boot"),
    db: Session = Depends(get_db)
):
    """
    Get PXE boot file (iPXE script) for a server based on MAC address.
    
    This endpoint is called by the DHCP server or iPXE client to retrieve
    the boot script for a specific server identified by its network port MAC address.
    
    Args:
        mac: MAC address of the network port (e.g., "00:0e:1e:6f:16:b0")
    
    Returns:
        iPXE boot script as plain text
    """
    try:
        # Validate MAC address
        if not mac:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="MAC address parameter is required"
            )
        # Normalize MAC address
        normalized_mac = normalize_mac_address(mac)
        
        # Find network port by MAC address
        port = NetworkPortDAO.get_by_mac_address(db, normalized_mac)
        
        if not port:
            logger.warning(f"PXE boot request for unknown MAC address: {mac} (normalized: {normalized_mac})")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No server found with MAC address: {mac}"
            )
        
        # Check if this port is configured for PXE boot
        if not port.pxe_boot:
            logger.warning(f"PXE boot requested for port {port.id} (MAC: {mac}) but pxe_boot is not enabled")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Network port with MAC {mac} is not configured for PXE boot"
            )
        
        # Get the server
        server = ServerDAO.get_by_id(db, port.server_id)
        if not server:
            logger.error(f"Server {port.server_id} not found for port {port.id}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server not found"
            )
        
        logger.info(f"Serving PXE boot file for server '{server.name}' (MAC: {mac}, Port: {port.name})")
        
        # Read the iPXE boot script file
        boot_script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "tftp", "pxe", "boot.ipxe"
        )
        
        if not os.path.exists(boot_script_path):
            logger.error(f"Boot script not found at {boot_script_path}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Boot script file not found"
            )
        
        with open(boot_script_path, "r") as f:
            boot_script = f.read()
        
        return PlainTextResponse(
            content=boot_script,
            media_type="text/plain",
            headers={
                "Content-Disposition": f'inline; filename="boot-{server.name}.ipxe"'
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving PXE boot file for MAC {mac}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to serve PXE boot file: {str(e)}"
        )


@router.get("/pxe/info")
async def get_pxe_info(
    mac: str = Query(..., description="MAC address of the network port"),
    db: Session = Depends(get_db)
):
    """
    Get PXE boot configuration information for a server.
    
    Returns information about the server and network port configuration
    without serving the actual boot file.
    
    Args:
        mac: MAC address of the network port (e.g., "00:0e:1e:6f:16:b0")
    """
    try:
        # Validate MAC address
        if not mac:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="MAC address parameter is required"
            )
        normalized_mac = normalize_mac_address(mac)
        
        port = NetworkPortDAO.get_by_mac_address(db, normalized_mac)
        
        if not port:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No server found with MAC address: {mac}"
            )
        
        server = ServerDAO.get_by_id(db, port.server_id)
        if not server:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server not found"
            )
        
        return {
            "server_id": server.id,
            "server_name": server.name,
            "server_ip": server.server_ip,
            "port_id": port.id,
            "port_name": port.name,
            "mac_address": port.mac_address,
            "pxe_boot": port.pxe_boot,
            "pxe_ip": port.pxe_ip,
            "boot_file_url": f"/api/servers/interaction/pxe?mac={normalized_mac}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting PXE info for MAC {mac}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get PXE info: {str(e)}"
        )
