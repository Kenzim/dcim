"""
API endpoints for server interactions (PXE boot, network config, password updates, etc.)

This module handles all server-to-API communication, including:
- PXE boot operations
- Network configuration retrieval
- Password updates
- Other server management operations
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request, Request
from fastapi.responses import PlainTextResponse, FileResponse, Response, Response
from sqlalchemy.orm import Session
from typing import Optional
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import NetworkPortDAO, ServerDAO, BootTaskDAO
from app.models.network_port import NetworkPort
from app.models.boot_task import BootType, BootTaskStatus
from app.services.os_template_service import get_template_service
from app.services.temp_os_service import get_temp_os_service
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
        "BC:24:11:9A:6B:9D@" -> "BC:24:11:9A:6B:9D" (strips trailing @)
    
    Raises:
        ValueError: If mac is None or empty
    """
    if not mac:
        raise ValueError("MAC address cannot be None or empty")
    
    # Remove separators and any trailing non-hex characters (like @ from kernel params)
    mac_clean = mac.replace(":", "").replace("-", "").replace(".", "").upper()
    
    # Strip any trailing non-hexadecimal characters (kernel params sometimes add @ or other chars)
    import re
    mac_clean = re.sub(r'[^0-9A-F].*$', '', mac_clean)
    
    # Add colons every 2 characters
    if len(mac_clean) == 12:
        return ":".join(mac_clean[i:i+2] for i in range(0, 12, 2))
    
    return mac.upper()


@router.get("/pxe", response_class=PlainTextResponse)
async def get_pxe_boot_file(
    mac: Optional[str] = Query(None, description="MAC address of the network port requesting PXE boot"),
    script: Optional[bool] = Query(False, description="Return script content instead of iPXE script (for initramfs)"),
    db: Session = Depends(get_db)
):
    """
    Get PXE boot file (iPXE script) or script content for a server based on MAC address.
    
    This endpoint is called by:
    - DHCP server or iPXE client: Returns iPXE boot script
    - Custom initramfs: Returns script content directly (use ?script=true)
    
    Args:
        mac: MAC address of the network port (e.g., "00:0e:1e:6f:16:b0")
        script: If true, return script content instead of iPXE script (for initramfs)
    
    Returns:
        iPXE boot script as plain text, or script content if script=true
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
        
        # Check if there's an active boot task for this server (pending or in_progress)
        boot_task = BootTaskDAO.get_active_by_server(db, server.id)
        
        # If script=true parameter, return script content directly (for custom initramfs)
        # If script=true but no boot task or wrong type, return 404
        if script:
            if not boot_task:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No active boot task found for script request"
                )
            # Serve script content for LINUX_SCRIPT or TEMP_OS (Alpine) boot tasks
            if boot_task.boot_type not in [BootType.LINUX_SCRIPT, BootType.TEMP_OS]:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No script content available for boot type {boot_task.boot_type.value}"
                )
            if boot_task.script_content:
                # Mark as in_progress when serving script content
                if boot_task.status == BootTaskStatus.PENDING:
                    BootTaskDAO.mark_in_progress(db, boot_task.id)
                logger.info(f"Serving script content for boot task {boot_task.id} (MAC: {mac})")
                return PlainTextResponse(
                    content=boot_task.script_content,
                    media_type="text/plain",
                    headers={
                        "Content-Disposition": f'inline; filename="script-{boot_task.id}.sh"'
                    }
                )
            elif boot_task.script_url:
                # If script_url is set, redirect to it or fetch and serve
                logger.info(f"Boot task {boot_task.id} has script_url, serving via redirect")
                # For now, return a message - Alpine can fetch from script_url directly
                raise HTTPException(
                    status_code=status.HTTP_302_FOUND,
                    detail=f"Script available at: {boot_task.script_url}"
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No script content for boot task {boot_task.id}"
                )
        
        if boot_task:
            if boot_task.boot_type == BootType.LINUX_SCRIPT:
                # For Linux script boots, mark as in_progress when serving the boot script
                # The script can report back via API when it completes
                if boot_task.status == BootTaskStatus.PENDING:
                    BootTaskDAO.mark_in_progress(db, boot_task.id)
                    # Refresh the task to get updated status
                    db.refresh(boot_task)
                # Build kernel and initrd URLs (use defaults if not specified)
                kernel_url = boot_task.kernel_url or "http://192.168.12.74:8000/api/servers/interaction/kernel/vmlinuz"
                initrd_url = boot_task.initrd_url or "http://192.168.12.74:8000/api/servers/interaction/initrd/initrd.gz"
                
                # Build script URL (use task's script_url or generate one)
                if boot_task.script_url:
                    script_url = boot_task.script_url
                elif boot_task.script_content:
                    # Script is stored in database, serve it via API
                    script_url = f"http://192.168.12.74:8000/api/servers/interaction/scripts/{boot_task.id}"
                else:
                    script_url = None
                
                # Build kernel parameters
                kernel_params = boot_task.kernel_params or ""
                if script_url:
                    kernel_params += f" script_url={script_url}"
                
                # Generate iPXE script to boot Linux
                boot_script = f"""#!ipxe
echo ========================================
echo   Booting Linux for script execution
echo ========================================
echo Server: {server.name}
echo Task ID: {boot_task.id}
echo
echo Loading kernel...
kernel {kernel_url} {kernel_params}
echo Loading initrd...
initrd {initrd_url}
echo Booting...
boot
"""
                logger.info(f"Boot task {boot_task.id} found for server {server.name}, booting Linux")
            
            elif boot_task.boot_type == BootType.ISO:
                # Boot from ISO image
                if not boot_task.iso_url:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="ISO URL is required for ISO boot type"
                    )
                
                # For ISO boots, mark as completed immediately after serving the boot script
                # We can't detect when an ISO boot completes, so we mark it done right away
                # This ensures it only boots once and doesn't show as active in the GUI
                if boot_task.status == BootTaskStatus.PENDING:
                    # Generate iPXE script to boot from ISO
                    # iPXE can boot ISOs directly using initrd + chain, or sanboot for disk-like ISOs
                    boot_script = f"""#!ipxe
echo ========================================
echo   Booting from ISO image
echo ========================================
echo Server: {server.name}
echo Task ID: {boot_task.id}
echo ISO: {boot_task.iso_url}
echo
echo Loading ISO into memory...
initrd {boot_task.iso_url}
echo Booting ISO...
# Try different ISO boot methods
chain {boot_task.iso_url} || sanboot --no-describe {boot_task.iso_url} || exit
"""
                    logger.info(f"Boot task {boot_task.id} found for server {server.name}, booting ISO: {boot_task.iso_url}")
                    # Mark as completed immediately since we can't track ISO boot completion
                    BootTaskDAO.mark_completed(db, boot_task.id)
                else:
                    # Already served/completed, return exit script
                    boot_script = """#!ipxe
echo ISO boot task already completed, booting from local disk...
exit
"""
            
            elif boot_task.boot_type == BootType.TEMP_OS:
                # Boot temporary OS (Alpine, SystemRescue, etc.)
                # For ALPINE type (deprecated), treat as temp_os with id 'alpine'
                temp_os_id = boot_task.temp_os_id
                if not temp_os_id:
                    temp_os_id = "alpine"  # Backward compatibility
                
                if not temp_os_id:
                    logger.error(f"Boot task {boot_task.id} has TEMP_OS type but no temp_os_id")
                    boot_script = """#!ipxe
echo Error: Temporary OS ID not specified
exit
"""
                else:
                    temp_os_service = get_temp_os_service()
                    os_config = temp_os_service.get_os_config(temp_os_id)
                    
                    if not os_config:
                        logger.error(f"Temporary OS '{temp_os_id}' not found for boot task {boot_task.id}")
                        boot_script = f"""#!ipxe
echo Error: Temporary OS '{temp_os_id}' not found
exit
"""
                    else:
                        # Get URLs from temp OS service
                        base_url = "http://192.168.12.74:8000"
                        # NOTE: For our squashfs-based live OS, we intentionally boot the kernel+initramfs
                        # from the `images/` directory (served by `/api/servers/interaction/images/...`).
                        # This avoids accidentally booting a distro initrd (e.g. Ubuntu casper) that cannot
                        # find our squashfs rootfs and will prompt "Attempt interactive netboot from a URL?".
                        # Get URLs from temp OS service (now serves from temp_os/{os_id}/files/)
                        kernel_url = boot_task.kernel_url or temp_os_service.get_kernel_url(temp_os_id, base_url)
                        initrd_url = boot_task.initrd_url or temp_os_service.get_initrd_url(temp_os_id, base_url)
                        kernel_params = boot_task.kernel_params or temp_os_service.get_kernel_params(temp_os_id)
                        
                        # Add squashfs fetch URL if available (for debian-live)
                        squashfs_url = temp_os_service.get_squashfs_url(temp_os_id, base_url)
                        if squashfs_url and "fetch=" not in kernel_params:
                            kernel_params = f"{kernel_params} fetch={squashfs_url}"
                        
                        # Add script URL if script exists
                        if boot_task.script_content:
                            script_url = f"{base_url}/api/servers/interaction/scripts/{boot_task.id}"
                            from urllib.parse import quote
                            encoded_script_url = quote(script_url, safe=':/?=&')
                            if "script_url=" not in kernel_params:
                                kernel_params = f"{kernel_params} script_url={encoded_script_url}"
                        
                        # Generate iPXE script for temporary OS
                        if boot_task.status == BootTaskStatus.PENDING:
                            boot_script = f"""#!ipxe
echo ========================================
echo   Booting {os_config.name}
echo ========================================
echo Server: {server.name}
echo Task ID: {boot_task.id}
echo OS: {os_config.name} ({os_config.version or 'unknown version'})
echo
echo Loading kernel...
echo   {kernel_url}
kernel {kernel_url} {kernel_params}
echo Loading initrd...
echo   {initrd_url}
initrd {initrd_url}
echo Booting {os_config.name}...
boot
"""
                            logger.info(f"Boot task {boot_task.id} found for server {server.name}, booting {os_config.name} (temp_os_id={temp_os_id})")
                            BootTaskDAO.mark_completed(db, boot_task.id)
                        else:
                            boot_script = f"""#!ipxe
echo {os_config.name} boot task already completed, booting from local disk...
exit
"""
            
            else:
                # Unknown boot type, fall back to local disk
                boot_script = """#!ipxe
echo Unknown boot type, booting from local disk...
exit
"""
        else:
            # No boot task, exit iPXE to boot from local disk
            boot_script = """#!ipxe
echo Exiting iPXE to boot from local disk...
exit
"""
        
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


# Boot Task Management Endpoints

class BootTaskCreate(BaseModel):
    """Request model for creating a boot task"""
    boot_type: str = "linux_script"
    kernel_url: Optional[str] = None
    initrd_url: Optional[str] = None
    kernel_params: Optional[str] = None
    script_url: Optional[str] = None
    script_content: Optional[str] = None
    # ISO boot configuration (for ISO type)
    iso_url: Optional[str] = None  # URL to ISO image
    # Temporary OS configuration (for TEMP_OS type)
    temp_os_id: Optional[str] = None  # ID of temporary OS (e.g., 'alpine', 'systemrescue')
    # Custom script configuration (for running scripts from scripts/ folder)
    custom_script: Optional[str] = None  # Filename of script in scripts/ folder
    # Description
    description: Optional[str] = None  # Optional description
    # Template-based installation (creates InstallationTask)
    template_id: Optional[str] = None  # Template ID to use
    template_parameters: Optional[dict] = None  # Parameters for template


class BootTaskResponse(BaseModel):
    """Response model for boot task"""
    id: int
    server_id: int
    boot_type: str
    kernel_url: Optional[str]
    initrd_url: Optional[str]
    kernel_params: Optional[str]
    script_url: Optional[str]
    temp_os_id: Optional[str] = None
    description: Optional[str] = None
    status: str
    error_message: Optional[str]
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    
    class Config:
        from_attributes = True


@router.post("/{server_id}/boot-task", response_model=BootTaskResponse)
async def create_boot_task(
    server_id: int,
    boot_task_data: BootTaskCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Create a boot task for a server.
    
    When a boot task exists, the server will boot into Linux and execute
    the specified script instead of booting from local disk.
    """
    # Verify server exists
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Server {server_id} not found"
        )
    
    # Handle template-based installation
    script_content = boot_task_data.script_content
    kernel_url = boot_task_data.kernel_url
    initrd_url = boot_task_data.initrd_url
    kernel_params = boot_task_data.kernel_params
    
    if boot_task_data.template_id:
        # Generate script from template
        template_service = get_template_service()
        template = template_service.get_template(boot_task_data.template_id)
        if not template:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template '{boot_task_data.template_id}' not found"
            )
        
        # Get template script
        script_path = template_service.get_template_script_path(boot_task_data.template_id)
        if not script_path or not script_path.exists():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Template '{boot_task_data.template_id}' missing installation script"
            )
        
        # Read script content
        with open(script_path, 'r') as f:
            script_content = f.read()
        
        # Use template's kernel/initrd URLs if not provided
        if template.kernel_url and not kernel_url:
            kernel_url = template.kernel_url
        if template.initrd_url and not initrd_url:
            initrd_url = template.initrd_url
        
        # Replace template variables in script
        # Get server's PXE boot port MAC address
        pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server_id)
        server_mac = pxe_port.mac_address if pxe_port else None
        
        # Build environment variables for script
        replacements = {
            "SERVER_IP": server.server_ip,
            "SERVER_MAC": server_mac or "",
            "SERVER_ID": str(server_id),
        }
        
        # Add disk image URL if template has one
        if template.disk_image:
            # Extract filename from disk_image path (e.g., "disk_images/windows.iso" -> "windows.iso")
            disk_image_filename = template.disk_image.split("/")[-1]
            disk_image_url = f"http://{server.server_ip}:8000/api/servers/interaction/disk-images/{disk_image_filename}"
            replacements["DISK_IMAGE_URL"] = disk_image_url
        
        # Add template parameters as PARAM_* variables
        if boot_task_data.template_parameters:
            for param_name, param_value in boot_task_data.template_parameters.items():
                replacements[f"PARAM_{param_name.upper()}"] = str(param_value)
        
        # Replace variables in script (simple ${VAR} replacement)
        for var_name, var_value in replacements.items():
            script_content = script_content.replace(f"${{{var_name}}}", var_value)
            script_content = script_content.replace(f"${var_name}", var_value)
    
    # Handle temporary OS boot type
    if boot_task_data.boot_type.lower() == "temp_os":
        if not boot_task_data.temp_os_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="temp_os_id is required for temp_os boot type"
            )
        
        temp_os_service = get_temp_os_service()
        os_config = temp_os_service.get_os_config(boot_task_data.temp_os_id)
        if not os_config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Temporary OS '{boot_task_data.temp_os_id}' not found"
            )
        
        # Get URLs from temp OS service
        base_url = "http://192.168.12.74:8000"
        kernel_url = temp_os_service.get_kernel_url(boot_task_data.temp_os_id, base_url)
        initrd_url = temp_os_service.get_initrd_url(boot_task_data.temp_os_id, base_url)
        kernel_params = temp_os_service.get_kernel_params(boot_task_data.temp_os_id, boot_task_data.kernel_params)
        
        # Set description if not provided
        if not boot_task_data.description:
            boot_task_data.description = f"Boot into {os_config.name}"
    
    # Handle custom script from scripts/ folder
    if boot_task_data.custom_script:
        # Read the custom script file
        scripts_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "scripts"
        )
        script_path = os.path.join(scripts_dir, boot_task_data.custom_script)
        
        # Security: only allow files in the scripts directory
        if ".." in boot_task_data.custom_script or "/" in boot_task_data.custom_script:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid script filename"
            )
        
        if not os.path.exists(script_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Script file '{boot_task_data.custom_script}' not found in scripts/ directory"
            )
        
        # Only allow .sh files
        if not boot_task_data.custom_script.endswith('.sh'):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only .sh script files are allowed"
            )
        
        # Read script content
        with open(script_path, 'r') as f:
            script_content = f.read()
        
        # Get server's PXE boot port MAC address for script variables
        pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server_id)
        server_mac = pxe_port.mac_address if pxe_port else None
        
        # Replace variables in script
        script_content = script_content.replace("${SERVER_IP}", server.server_ip or "")
        script_content = script_content.replace("${SERVER_MAC}", server_mac or "")
        script_content = script_content.replace("${SERVER_ID}", str(server_id))
        script_content = script_content.replace("$SERVER_IP", server.server_ip or "")
        script_content = script_content.replace("$SERVER_MAC", server_mac or "")
        script_content = script_content.replace("$SERVER_ID", str(server_id))
        
        # Custom scripts boot Debian Live (squashfs)
        boot_type = BootType.TEMP_OS
        # Get temp OS config for debian-live
        temp_os_service = get_temp_os_service()
        os_config = temp_os_service.get_os_config("debian-live")
        if not os_config:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="debian-live temporary OS not found"
            )
        temp_os_id = "debian-live"
        
        # Get URLs - for debian-live, kernel/initrd are served from /api/servers/interaction/images/
        base_url = "http://192.168.12.74:8000"
        kernel_url = f"{base_url}/api/servers/interaction/images/{os_config.kernel_file}"
        initrd_url = f"{base_url}/api/servers/interaction/images/{os_config.initrd_file}"
        kernel_params = temp_os_service.get_kernel_params("debian-live", boot_task_data.kernel_params)
        # Script URL will be added to kernel params after boot task is created
        pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server_id)
        server_mac = pxe_port.mac_address if pxe_port else None
        if server_mac:
            base_url = "http://192.168.12.74:8000"
            preseed_url = f"{base_url}/api/servers/interaction/preseed?mac={server_mac}"
            kernel_params = f"{kernel_params} preseed/url={preseed_url}"
        
        # Set description if not provided
        if not boot_task_data.description:
            boot_task_data.description = f"Run custom script: {boot_task_data.custom_script}"
        
        # Set boot_type for custom script
        boot_type = BootType.TEMP_OS
    elif boot_task_data.boot_type.lower() == "temp_os":
        boot_type = BootType.TEMP_OS
        temp_os_id = boot_task_data.temp_os_id
    else:
        boot_type = BootType(boot_task_data.boot_type.lower())
        temp_os_id = boot_task_data.temp_os_id
    
    # Create boot task
    boot_task = BootTaskDAO.create(
        db=db,
        server_id=server_id,
        boot_type=boot_type,
        kernel_url=kernel_url,
        initrd_url=initrd_url,
        kernel_params=kernel_params,
        script_url=script_url if 'script_url' in locals() else boot_task_data.script_url,
        script_content=script_content,
        iso_url=boot_task_data.iso_url,
        temp_os_id=temp_os_id,
        description=boot_task_data.description
    )
    
    # For Debian Live (squashfs), add script URL to kernel params if script exists
    if boot_task.temp_os_id == "debian-live" and boot_task.script_content:
        base_url = "http://192.168.12.74:8000"
        script_url = f"{base_url}/api/servers/interaction/scripts/{boot_task.id}"
        from urllib.parse import quote
        encoded_script_url = quote(script_url, safe=':/?=&')
        if "script_url=" not in boot_task.kernel_params:
            updated_kernel_params = f"{boot_task.kernel_params} script_url={encoded_script_url}"
            boot_task.kernel_params = updated_kernel_params
            db.commit()
            db.refresh(boot_task)
    
    # If this is a template-based installation, create InstallationTask
    installation_task = None
    if boot_task_data.template_id:
        from app.dao.installation_task_dao import InstallationTaskDAO
        template_service = get_template_service()
        template = template_service.get_template(boot_task_data.template_id)
        
        installation_task = InstallationTaskDAO.create(
            db=db,
            server_id=server_id,
            boot_task_id=boot_task.id,
            template_id=boot_task_data.template_id,
            template_parameters=boot_task_data.template_parameters,
            os_name=template.name if template else None
        )
        logger.info(f"Created installation task {installation_task.id} for boot task {boot_task.id}")
    
    logger.info(f"Created boot task {boot_task.id} for server {server_id}")
    
    return BootTaskResponse(
        id=boot_task.id,
        server_id=boot_task.server_id,
        boot_type=boot_task.boot_type.value,
        kernel_url=boot_task.kernel_url,
        initrd_url=boot_task.initrd_url,
        kernel_params=boot_task.kernel_params,
        script_url=boot_task.script_url,
        temp_os_id=boot_task.temp_os_id,
        description=boot_task.description,
        status=boot_task.status.value,
        error_message=boot_task.error_message,
        created_at=boot_task.created_at.isoformat() if boot_task.created_at else None,
        started_at=boot_task.started_at.isoformat() if boot_task.started_at else None,
        completed_at=boot_task.completed_at.isoformat() if boot_task.completed_at else None
    )


@router.get("/{server_id}/boot-task", response_model=Optional[BootTaskResponse])
async def get_boot_task(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get the active boot task for a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Server {server_id} not found"
        )
    
    boot_task = BootTaskDAO.get_active_by_server(db, server_id)
    if not boot_task:
        return None
    
    return BootTaskResponse(
        id=boot_task.id,
        server_id=boot_task.server_id,
        boot_type=boot_task.boot_type.value,
        kernel_url=boot_task.kernel_url,
        initrd_url=boot_task.initrd_url,
        kernel_params=boot_task.kernel_params,
        script_url=boot_task.script_url,
        temp_os_id=boot_task.temp_os_id,
        description=boot_task.description,
        status=boot_task.status.value,
        error_message=boot_task.error_message,
        created_at=boot_task.created_at.isoformat() if boot_task.created_at else None,
        started_at=boot_task.started_at.isoformat() if boot_task.started_at else None,
        completed_at=boot_task.completed_at.isoformat() if boot_task.completed_at else None
    )


@router.delete("/{server_id}/boot-task")
async def cancel_boot_task(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Cancel any pending boot tasks for a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Server {server_id} not found"
        )
    
    count = BootTaskDAO.cancel_pending_tasks(db, server_id)
    logger.info(f"Cancelled {count} boot task(s) for server {server_id}")
    
    return {"cancelled": count}


@router.get("/scripts/{task_id}", response_class=PlainTextResponse)
async def get_script(
    task_id: int,
    db: Session = Depends(get_db)
):
    """
    Serve script content for a boot task.
    
    This endpoint is called by the Linux environment to fetch the script
    that should be executed.
    """
    boot_task = BootTaskDAO.get_by_id(db, task_id)
    if not boot_task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Boot task {task_id} not found"
        )
    
    if not boot_task.script_content:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No script content for boot task {task_id}"
        )
    
    logger.info(f"Serving script for boot task {task_id}")
    
    return PlainTextResponse(
        content=boot_task.script_content,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'inline; filename="script-{task_id}.sh"'
        }
    )


# Kernel and Initrd Serving Endpoints

@router.get("/kernel/{filename}")
async def get_kernel(
    filename: str,
    db: Session = Depends(get_db)
):
    """
    Serve Linux kernel files.
    
    This endpoint serves kernel files (vmlinuz) for network booting.
    Place kernel files in tftp/pxe/kernel/ directory.
    """
    kernel_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "tftp", "pxe", "kernel", filename
    )
    
    # Security: only allow files in the kernel directory
    if ".." in filename or "/" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )
    
    if not os.path.exists(kernel_path):
        logger.warning(f"Kernel file not found: {filename}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Kernel file not found: {filename}. Place kernel files in tftp/pxe/kernel/"
        )
    
    logger.info(f"Serving kernel file: {filename}")
    
    return FileResponse(
        kernel_path,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )


@router.get("/initrd/{filename}")
async def get_initrd(
    filename: str,
    db: Session = Depends(get_db)
):
    """
    Serve initrd/initramfs files.
    
    This endpoint serves initrd files for network booting.
    Place initrd files in tftp/pxe/initrd/ directory.
    """
    initrd_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "tftp", "pxe", "initrd", filename
    )
    
    # Security: only allow files in the initrd directory
    if ".." in filename or "/" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )
    
    if not os.path.exists(initrd_path):
        logger.warning(f"Initrd file not found: {filename}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Initrd file not found: {filename}. Place initrd files in tftp/pxe/initrd/"
        )
    
    logger.info(f"Serving initrd file: {filename}")
    
    return FileResponse(
        initrd_path,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )


# Custom Scripts Endpoints

@router.get("/scripts")
async def list_scripts(
    auth: dict = Depends(require_admin)
):
    """
    List all available custom scripts.
    
    Returns a list of script files available in the scripts/ directory.
    """
    scripts_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "scripts"
    )
    
    if not os.path.exists(scripts_dir):
        return []
    
    script_files = []
    for filename in os.listdir(scripts_dir):
        file_path = os.path.join(scripts_dir, filename)
        if os.path.isfile(file_path) and filename.endswith('.sh'):
            file_size = os.path.getsize(file_path)
            script_files.append({
                "filename": filename,
                "size_bytes": file_size,
                "size_kb": round(file_size / 1024, 2),
                "url": f"/api/servers/interaction/scripts/{filename}"
            })
    
    return sorted(script_files, key=lambda x: x["filename"])


@router.get("/scripts/{filename}")
async def get_script(
    filename: str,
    db: Session = Depends(get_db)
):
    """
    Serve custom script files.
    
    This endpoint serves script files from the scripts/ directory.
    Scripts can be executed via boot tasks.
    """
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "scripts", filename
    )
    
    # Security: only allow files in the scripts directory
    if ".." in filename or "/" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )
    
    if not os.path.exists(script_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Script '{filename}' not found"
        )
    
    # Only allow .sh files
    if not filename.endswith('.sh'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .sh script files are allowed"
        )
    
    logger.info(f"Serving script file: {filename}")
    return FileResponse(
        script_path,
        media_type="text/x-shellscript",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )


# ISO Serving Endpoints

@router.get("/isos")
async def list_isos(
    auth: dict = Depends(require_admin)
):
    """
    List all available ISO files.
    
    Returns a list of ISO files available in the isos/ directory.
    """
    isos_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "isos"
    )
    
    if not os.path.exists(isos_dir):
        return []
    
    iso_files = []
    for filename in os.listdir(isos_dir):
        file_path = os.path.join(isos_dir, filename)
        if os.path.isfile(file_path) and filename.lower().endswith('.iso'):
            file_size = os.path.getsize(file_path)
            iso_files.append({
                "filename": filename,
                "size_bytes": file_size,
                "size_mb": round(file_size / (1024 * 1024), 2),
                "url": f"http://192.168.12.74:8000/api/servers/interaction/isos/{filename}"
            })
    
    return sorted(iso_files, key=lambda x: x["filename"])


@router.get("/isos/{filename}")
@router.head("/isos/{filename}")
async def get_iso(
    filename: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Serve ISO image files.
    
    This endpoint serves ISO files for network booting.
    Place ISO files in the isos/ directory.
    Supports both GET and HEAD requests (HEAD for file size checks).
    """
    iso_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "isos", filename
    )
    
    # Security: only allow files in the isos directory
    if ".." in filename or "/" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )
    
    if not os.path.exists(iso_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ISO file '{filename}' not found"
        )
    
    # For HEAD requests, return headers only (no body)
    if request.method == "HEAD":
        file_size = os.path.getsize(iso_path)
        return Response(
            status_code=200,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(file_size),
                "Content-Disposition": f'inline; filename="{filename}"'
            }
        )
    
    logger.info(f"Serving ISO file: {filename}")
    return FileResponse(
        iso_path,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )


# Temporary OS Endpoints

@router.get("/temp-os")
async def list_temp_os(
    db: Session = Depends(get_db)
):
    """
    List all available temporary OSes.
    
    Returns a list of temporary OS configurations that can be booted.
    """
    temp_os_service = get_temp_os_service()
    configs = temp_os_service.scan_os_configs()
    
    return [
        {
            "id": config.id,
            "name": config.name,
            "description": config.description,
            "version": config.version,
            "flavor": config.flavor,
        }
        for config in configs
    ]


@router.get("/temp-os/{os_id}/files/{filename}")
@router.head("/temp-os/{os_id}/files/{filename}")
async def get_temp_os_file(
    os_id: str,
    filename: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Serve files for temporary OSes from their directory.
    
    This endpoint serves files (kernel, initrd, squashfs, etc.) from the temp_os/{os_id}/ directory.
    Files are stored directly in the temp_os/{os_id}/ folder (not in subdirectories).
    """
    temp_os_service = get_temp_os_service()
    os_dir = temp_os_service.get_os_dir(os_id)
    
    if not os_dir:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Temporary OS '{os_id}' not found"
        )
    
    file_path = os_dir / filename
    
    # Security: only allow files in the temp OS directory
    if ".." in filename or "/" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )
    
    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File '{filename}' not found for OS '{os_id}'"
        )
    
    # Determine media type based on extension
    if filename.endswith('.squashfs'):
        media_type = "application/x-squashfs"
    elif filename.endswith('.img') or filename.endswith('.cpio.gz') or filename.endswith('.cpio'):
        media_type = "application/x-cpio"
    elif filename.startswith('vmlinuz') or filename.endswith('.bin') or filename.endswith('.efi'):
        media_type = "application/x-executable"
    else:
        media_type = "application/octet-stream"
    
    logger.info(f"Serving file: {os_id}/{filename}")
    return FileResponse(
        str(file_path),
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )


# Live OS Image Serving Endpoints

@router.get("/images/{filename}")
@router.head("/images/{filename}")
async def get_live_os_image(
    filename: str,
    request: Request
):
    """
    Serve live OS images (squashfs, initramfs, kernel) from images/ directory.
    
    This endpoint serves:
    - debian-live.squashfs (the root filesystem)
    - initramfs-live.cpio.gz (the initramfs)
    - vmlinuz-live (the kernel)
    
    Supports both GET and HEAD requests.
    """
    images_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "images"
    )
    
    image_path = os.path.join(images_dir, filename)
    
    # Security: only allow files in the images directory
    if ".." in filename or "/" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )
    
    if not os.path.exists(image_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Image file '{filename}' not found"
        )
    
    # Determine media type based on extension
    if filename.endswith('.squashfs'):
        media_type = "application/x-squashfs"
    elif filename.endswith('.cpio.gz') or filename.endswith('.cpio'):
        media_type = "application/x-cpio"
    elif filename.startswith('vmlinuz') or filename.endswith('.bin'):
        media_type = "application/x-executable"
    else:
        media_type = "application/octet-stream"
    
    logger.info(f"Serving live OS image: {filename}")
    return FileResponse(
        image_path,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )




# Disk Image Serving Endpoints

@router.get("/disk-images/{filename}")
@router.head("/disk-images/{filename}")
async def get_disk_image(
    filename: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Serve disk image files (ISOs, disk images, etc.) from disk_images/ directory.
    
    This endpoint serves disk images for OS installation templates.
    Place disk image files in the disk_images/ directory.
    Supports both GET and HEAD requests (HEAD for file size checks).
    """
    disk_image_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "disk_images", filename
    )
    
    # Security: only allow files in the disk_images directory
    if ".." in filename or "/" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )
    
    if not os.path.exists(disk_image_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Disk image '{filename}' not found"
        )
    
    # For HEAD requests, return headers only (no body)
    if request.method == "HEAD":
        file_size = os.path.getsize(disk_image_path)
        return Response(
            status_code=200,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(file_size),
                "Content-Disposition": f'inline; filename="{filename}"'
            }
        )
    
    logger.info(f"Serving disk image file: {filename}")
    return FileResponse(
        disk_image_path,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )
