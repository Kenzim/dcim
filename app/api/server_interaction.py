"""
API endpoints for server interactions (PXE boot, network config, password updates, etc.)

This module handles all server-to-API communication, including:
- PXE boot operations
- Network configuration retrieval
- Password updates
- Other server management operations
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from fastapi.responses import PlainTextResponse, FileResponse, Response
from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from app.core.database import get_db
from app.core.auth import require_admin, get_current_user
from app.dao import (
    NetworkPortDAO,
    ServerDAO,
    BootTaskDAO,
    DiskDAO,
    ServiceInstanceDAO,
    InstallationTaskDAO,
    HardwareDetectionReportDAO,
)
from app.models.network_port import NetworkPort
from app.models.boot_task import BootType, BootTaskStatus
from app.models.hardware_detection_report import HardwareDetectionReportStatus
from app.services.os_template_service import get_template_service
from app.services.temp_os_service import get_temp_os_service
from app.services.download_token_service import get_download_token_service
from app.services.dhcp_config_service import get_dhcp_config_service
from app.services.dhcp_config_generator import get_next_server_ip_for_client, get_subnet_info_for_client
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_success,
)
from app.models.server_activity import ServerActivityEventType
import logging
import re
import os

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_base_url_for_pxe_ip(db, pxe_ip: Optional[str]) -> str:
    """
    Base URL for boot resources (ISO, kernel, initrd, etc.).

    Prefer the per-location DHCP configuration used for this server's location,
    so the IP matches what we configured in the DHCP settings for that location.
    Falls back to the legacy global DHCP config when no per-location config
    can be resolved.
    """
    dhcp_svc = get_dhcp_config_service()
    config = None

    # When we know the PXE IP, try to find the server and its location so we
    # can use that location's DHCP service instance config.
    if pxe_ip:
        try:
            port = db.query(NetworkPort).filter(NetworkPort.pxe_ip == pxe_ip).first()
        except Exception:
            port = None
        if port and port.server_id:
            server = ServerDAO.get_by_id(db, port.server_id)
            location_id = getattr(server, "location_id", None) if server else None
            if location_id:
                instance = ServiceInstanceDAO.get_by_location_and_type(
                    db, location_id, "dhcp"
                )
                if instance:
                    config = dhcp_svc.get_config_by_service_instance(db, instance.id)

    # Fallback to global config when no per-location config is available
    if config is None:
        config = dhcp_svc.get_config(db)

    ip = get_next_server_ip_for_client(config, pxe_ip)
    return f"http://{ip}:8000"


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
        base_url = _get_base_url_for_pxe_ip(db, port.pxe_ip)
        
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

            # When script=true but there is no inline script_content (even if script_url is set),
            # treat this as "no script content available" for the purposes of the initramfs client.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No script content for boot task {boot_task.id}"
            )
        
        if boot_task:
            # Basic DHCP only
            ip_param = "ip=dhcp"

            if boot_task.boot_type == BootType.LINUX_SCRIPT:
                # For Linux script boots, mark as in_progress when serving the boot script
                # The script can report back via API when it completes
                if boot_task.status == BootTaskStatus.PENDING:
                    BootTaskDAO.mark_in_progress(db, boot_task.id)
                    # Refresh the task to get updated status
                    db.refresh(boot_task)
                # Build kernel and initrd URLs (use defaults if not specified)
                kernel_url = boot_task.kernel_url or f"{base_url}/api/servers/interaction/kernel/vmlinuz"
                initrd_url = boot_task.initrd_url or f"{base_url}/api/servers/interaction/initrd/initrd.gz"
                
                # Build script URL (use task's script_url or generate one)
                if boot_task.script_url:
                    script_url = boot_task.script_url
                elif boot_task.script_content:
                    # Script is stored in database, serve it via API
                    script_url = f"{base_url}/api/servers/interaction/scripts/{boot_task.id}"
                else:
                    script_url = None
                
                # Build kernel parameters (static IP + MAC pinning or MAC-based DHCP)
                kernel_params = boot_task.kernel_params or ""
                if ip_param is not None:
                    orig = kernel_params
                    kernel_params = re.sub(r"\bip=[^\s]+", ip_param, kernel_params, count=1)
                    if kernel_params == orig:
                        kernel_params = f"{kernel_params} {ip_param}".strip()
                    else:
                        kernel_params = kernel_params.strip()
                if script_url:
                    kernel_params += f" script_url={script_url}"
                if "rd.neednet=" not in kernel_params:
                    kernel_params = f"{kernel_params} rd.neednet=1".strip()

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
                # Use next-server base URL so the client can reach the ISO (same subnet as DHCP)
                from urllib.parse import urlparse
                _u = urlparse(boot_task.iso_url)
                iso_filename = os.path.basename(_u.path.rstrip("/"))
                if not iso_filename:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid ISO URL: no filename in path"
                    )
                # Generate one-time download token so the client can fetch the ISO (endpoint requires token)
                download_token_service = get_download_token_service()
                iso_token = download_token_service.generate_token(
                    boot_task_id=boot_task.id,
                    allowed_files=[iso_filename],
                    expires_in=900,
                    single_use=False,  # iPXE requests same URL for initrd and again for chain/sanboot
                )
                iso_url_to_use = f"{base_url}{_u.path}?token={iso_token}"
                
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
echo ISO: {iso_url_to_use}
echo
echo Loading ISO into memory...
initrd {iso_url_to_use}
echo Booting ISO...
# Try different ISO boot methods
chain {iso_url_to_use} || sanboot --no-describe {iso_url_to_use} || exit
"""
                    logger.info(f"Boot task {boot_task.id} found for server {server.name}, booting ISO: {iso_url_to_use}")
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
                        # Get URLs from temp OS service (base_url already set from port.pxe_ip above)
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
                        
                        # Pin by MAC: static IP or MAC-based DHCP (use ip_param computed above)
                        orig = kernel_params
                        kernel_params = re.sub(r"\bip=[^\s]+", ip_param, kernel_params, count=1)
                        if kernel_params == orig:
                            kernel_params = f"{kernel_params} {ip_param}".strip()
                        else:
                            kernel_params = kernel_params.strip()
                        # Add script URL if script exists
                        if boot_task.script_content:
                            script_url = f"{base_url}/api/servers/interaction/scripts/{boot_task.id}"
                            from urllib.parse import quote
                            encoded_script_url = quote(script_url, safe=':/?=&')
                            if "script_url=" not in kernel_params:
                                kernel_params = f"{kernel_params} script_url={encoded_script_url}"
                        if "rd.neednet=" not in kernel_params:
                            kernel_params = f"{kernel_params} rd.neednet=1".strip()

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


def _get_request_source_ip(request: Request) -> Optional[str]:
    """Get caller IP, preferring X-Forwarded-For when present."""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_ip = forwarded_for.split(",")[0].strip()
        if first_ip:
            return first_ip
    if request.client:
        return request.client.host
    return None


def _resolve_server_by_source_ip(db: Session, source_ip: Optional[str]):
    """Resolve server from source IP using PXE IP first, then server primary IP."""
    if not source_ip:
        return None

    pxe_port = NetworkPortDAO.get_by_pxe_ip(db, source_ip)
    if pxe_port and pxe_port.server_id:
        server = ServerDAO.get_by_id(db, pxe_port.server_id)
        if server:
            return server

    return ServerDAO.get_by_ip(db, source_ip)


def _get_cloud_init_installation_context(db: Session, server_id: int):
    """Find installation context to render cloud-init data."""
    active_boot_task = BootTaskDAO.get_active_by_server(db, server_id)
    if active_boot_task:
        by_boot_task = InstallationTaskDAO.get_by_boot_task(db, active_boot_task.id)
        if by_boot_task:
            return by_boot_task

    active_install = InstallationTaskDAO.get_active_by_server(db, server_id)
    if active_install:
        return active_install

    history = InstallationTaskDAO.get_by_server(db, server_id)
    return history[0] if history else None


def _safe_yaml_single_quoted(value: str) -> str:
    text = str(value).replace("\r", "").replace("\n", " ").strip()
    return "'" + text.replace("'", "''") + "'"


def _sanitize_username(value: Optional[str]) -> str:
    candidate = (value or "").strip().lower()
    if re.match(r"^[a-z_][a-z0-9_-]{0,31}$", candidate):
        return candidate
    return "rackflow"


def _sanitize_hostname(value: Optional[str], fallback: str = "rackflow-host") -> str:
    raw = (value or "").strip().lower()
    if not raw:
        raw = fallback
    raw = re.sub(r"[^a-z0-9-]", "-", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    if not raw:
        raw = fallback
    return raw[:63]


def _build_cloud_init_user_data(server, installation_task) -> str:
    params = installation_task.template_parameters or {}

    username = _sanitize_username(params.get("username"))
    password = (params.get("password") or "").strip()
    ssh_public_key = (params.get("ssh_public_key") or "").replace("\r", "").strip()
    hostname = _sanitize_hostname(getattr(server, "name", None), fallback=f"rackflow-{server.id}")

    if not password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Installation parameters are missing required 'password' for cloud-init rendering",
        )

    lines = [
        "#cloud-config",
        "preserve_hostname: false",
        f"hostname: {hostname}",
        "manage_etc_hosts: true",
        "users:",
        "  - default",
        "  - name: " + _safe_yaml_single_quoted(username),
        "    shell: /bin/bash",
        "    lock_passwd: false",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "    groups: [adm, sudo]",
    ]

    if ssh_public_key:
        lines.extend(
            [
                "    ssh_authorized_keys:",
                "      - " + _safe_yaml_single_quoted(ssh_public_key),
            ]
        )

    lines.extend(
        [
            "chpasswd:",
            "  expire: false",
            "  users:",
            "    - name: " + _safe_yaml_single_quoted(username),
            "      password: " + _safe_yaml_single_quoted(password),
            "ssh_pwauth: true",
            "package_update: false",
            "package_upgrade: false",
            "final_message: " + _safe_yaml_single_quoted(f"Rackflow cloud-init completed for server {server.id}"),
        ]
    )
    return "\n".join(lines) + "\n"


def _build_cloud_init_meta_data(server, installation_task) -> str:
    hostname = _sanitize_hostname(getattr(server, "name", None), fallback=f"rackflow-{server.id}")
    install_id = getattr(installation_task, "id", "unknown")
    instance_id = f"rackflow-{server.id}-{install_id}"
    return (
        f"instance-id: {instance_id}\n"
        f"local-hostname: {hostname}\n"
    )


class HardwareNicEntry(BaseModel):
    name: str
    mac_address: Optional[str] = None
    speed_mbps: Optional[int] = None
    model: Optional[str] = None
    pci_address: Optional[str] = None
    is_physical: bool = True


class HardwareDiskEntry(BaseModel):
    name: Optional[str] = None
    serial_number: Optional[str] = None
    model: Optional[str] = None
    capacity_gb: Optional[int] = None
    type: Optional[str] = None
    is_os_disk: bool = False


class HardwareDetectionReportIngest(BaseModel):
    cpu_count: Optional[int] = None
    cpu_model: Optional[str] = None
    ram_gb: Optional[int] = None
    nics: List[HardwareNicEntry] = Field(default_factory=list)
    disks: List[HardwareDiskEntry] = Field(default_factory=list)
    detector_version: Optional[str] = None


def _normalize_hardware_report_payload(payload: HardwareDetectionReportIngest) -> Dict[str, Any]:
    normalized_nics: List[Dict[str, Any]] = []
    for nic in payload.nics:
        if not nic.is_physical:
            continue
        if not nic.pci_address:
            continue
        normalized_nics.append(
            {
                "name": nic.name.strip() if nic.name else "unknown",
                "mac_address": (nic.mac_address or "").strip().lower() or None,
                "speed_mbps": nic.speed_mbps if nic.speed_mbps and nic.speed_mbps > 0 else None,
                "model": (nic.model or "").strip() or None,
                "pci_address": (nic.pci_address or "").strip().lower() or None,
                "is_physical": True,
            }
        )

    normalized_disks: List[Dict[str, Any]] = []
    for disk in payload.disks:
        size = disk.capacity_gb if disk.capacity_gb and disk.capacity_gb > 0 else None
        disk_type = (disk.type or "").strip().lower()
        if disk_type not in ("ssd", "hdd"):
            disk_type = None
        normalized_disks.append(
            {
                "name": (disk.name or "").strip() or None,
                "serial_number": (disk.serial_number or "").strip() or None,
                "model": (disk.model or "").strip() or None,
                "capacity_gb": size,
                "type": disk_type,
                "is_os_disk": bool(disk.is_os_disk),
            }
        )

    return {
        "cpu": {
            "count": payload.cpu_count if payload.cpu_count and payload.cpu_count > 0 else None,
            "model": (payload.cpu_model or "").strip() or None,
        },
        "ram_gb": payload.ram_gb if payload.ram_gb and payload.ram_gb > 0 else None,
        "nics": normalized_nics,
        "disks": normalized_disks,
        "detector_version": (payload.detector_version or "").strip() or None,
    }


@router.get("/cloud-init/user-data", response_class=PlainTextResponse)
async def get_cloud_init_user_data(
    request: Request,
    db: Session = Depends(get_db)
):
    """Serve generic cloud-init user-data for the server identified by requester IP."""
    source_ip = _get_request_source_ip(request)
    server = _resolve_server_by_source_ip(db, source_ip)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No server found for request source IP",
        )

    installation_task = _get_cloud_init_installation_context(db, server.id)
    if not installation_task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No installation context found for server",
        )

    return PlainTextResponse(
        content=_build_cloud_init_user_data(server, installation_task),
        media_type="text/plain",
    )


@router.get("/cloud-init/meta-data", response_class=PlainTextResponse)
async def get_cloud_init_meta_data(
    request: Request,
    db: Session = Depends(get_db)
):
    """Serve cloud-init meta-data for the server identified by requester IP."""
    source_ip = _get_request_source_ip(request)
    server = _resolve_server_by_source_ip(db, source_ip)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No server found for request source IP",
        )

    installation_task = _get_cloud_init_installation_context(db, server.id)
    if not installation_task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No installation context found for server",
        )

    return PlainTextResponse(
        content=_build_cloud_init_meta_data(server, installation_task),
        media_type="text/plain",
    )


@router.post("/hardware-detection/report", response_model=dict)
async def ingest_hardware_detection_report(
    payload: HardwareDetectionReportIngest,
    request: Request,
    token: str = Query(..., description="download token tied to detection boot task"),
    db: Session = Depends(get_db),
):
    """Receive hardware detection report from booted detection script."""
    token_service = get_download_token_service()
    token_data = token_service.validate_token(token, "hardware-detection-report-v1")
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired detection token",
        )

    boot_task = BootTaskDAO.get_by_id(db, token_data.get("boot_task_id"))
    if not boot_task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Boot task not found for detection token",
        )

    report = HardwareDetectionReportDAO.get_by_boot_task_id(db, boot_task.id)
    if not report:
        report = HardwareDetectionReportDAO.create(
            db,
            server_id=boot_task.server_id,
            boot_task_id=boot_task.id,
            status=HardwareDetectionReportStatus.PENDING,
        )

    normalized_payload = _normalize_hardware_report_payload(payload)
    source_ip = _get_request_source_ip(request)
    HardwareDetectionReportDAO.mark_submitted(
        db,
        report,
        detected_inventory=normalized_payload,
        source_ip=source_ip,
    )

    return {
        "status": "ok",
        "report_id": report.id,
        "server_id": report.server_id,
    }


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
    # Custom script configuration (for running scripts from database)
    custom_script: Optional[str] = None  # Script ID (integer) or name (string) from database
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
    activity_action = "queue_boot_task"
    activity_message = "Boot task requested"
    activity_details = {"boot_type": boot_task_data.boot_type.lower()}
    if boot_task_data.template_id:
        activity_action = "queue_template_install"
        activity_message = f"Template install requested for '{boot_task_data.template_id}'"
        activity_details["template_id"] = boot_task_data.template_id
    elif boot_task_data.custom_script:
        activity_action = "run_script"
        activity_message = f"Script run requested for '{boot_task_data.custom_script}'"
        activity_details["custom_script"] = boot_task_data.custom_script
    log_server_activity_attempt(
        db,
        server_id=server_id,
        event_type=ServerActivityEventType.INSTALL,
        action=activity_action,
        source="admin_api",
        message=activity_message,
        details=activity_details,
    )
    pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server_id)
    base_url = _get_base_url_for_pxe_ip(db, pxe_port.pxe_ip if pxe_port else None)
    
    # Initialize replacements dict (will be populated by template or other branches)
    replacements = {}
    
    # Handle template-based installation
    script_content = boot_task_data.script_content
    kernel_url = boot_task_data.kernel_url
    initrd_url = boot_task_data.initrd_url
    kernel_params = boot_task_data.kernel_params
    boot_type = None  # Will be set based on boot_task_data or template
    temp_os_id = boot_task_data.temp_os_id
    
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
        
        # Replace template variables in script
        # Get server's PXE boot port MAC address
        pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server_id)
        server_mac = pxe_port.mac_address if pxe_port else None
        
        # Get OS disk for installation
        os_disk = DiskDAO.get_os_disk(db, server_id)
        
        # Build environment variables for script
        replacements = {
            "SERVER_IP": server.server_ip,
            "SERVER_MAC": server_mac or "",
            "SERVER_ID": str(server_id),
            "OS_BOOT_MODE": server.os_boot_mode.value if server.os_boot_mode else "uefi",
        }
        
        # Add disk information for installation scripts
        if os_disk:
            replacements["OS_DISK_SERIAL"] = os_disk.serial_number or ""
            replacements["OS_DISK_SIZE_GB"] = str(os_disk.capacity_gb)
            replacements["OS_DISK_TYPE"] = os_disk.type.value.lower() if hasattr(os_disk.type, 'value') else str(os_disk.type).lower()
        else:
            replacements["OS_DISK_SERIAL"] = ""
            replacements["OS_DISK_SIZE_GB"] = ""
            replacements["OS_DISK_TYPE"] = ""
        
        # Template installations use temp_os boot type with debian-live (same as normal script runs)
        # This ensures scripts run properly via the bashrc/profile system
        boot_type = BootType.TEMP_OS
        temp_os_id = "debian-live"
        
        # Get URLs from debian-live temp OS service
        temp_os_service = get_temp_os_service()
        
        # Store disk image filename for token generation
        disk_image_filename = None
        if template.disk_image:
            disk_image_filename = template.disk_image.split("/")[-1]
        
        # Add template parameters as PARAM_* variables
        if boot_task_data.template_parameters:
            for param_name, param_value in boot_task_data.template_parameters.items():
                replacements[f"PARAM_{param_name.upper()}"] = str(param_value)
        
        # Note: Variable replacement will happen AFTER token generation (below)
        # This ensures all variables including DISK_IMAGE_URL and DOWNLOAD_TOKEN are replaced in one pass
        kernel_url = temp_os_service.get_kernel_url("debian-live", base_url)
        initrd_url = temp_os_service.get_initrd_url("debian-live", base_url)
        kernel_params = temp_os_service.get_kernel_params("debian-live")
        
        # Add squashfs fetch URL (required for debian-live to boot properly)
        squashfs_url = temp_os_service.get_squashfs_url("debian-live", base_url)
        if squashfs_url and "fetch=" not in kernel_params:
            kernel_params = f"{kernel_params} fetch={squashfs_url}"
    
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
        kernel_url = temp_os_service.get_kernel_url(boot_task_data.temp_os_id, base_url)
        initrd_url = temp_os_service.get_initrd_url(boot_task_data.temp_os_id, base_url)
        kernel_params = temp_os_service.get_kernel_params(boot_task_data.temp_os_id, boot_task_data.kernel_params)
        
        # Set description if not provided
        if not boot_task_data.description:
            boot_task_data.description = f"Boot into {os_config.name}"
    
    # Handle custom script from database
    if boot_task_data.custom_script:
        # Get script from database (by name or ID)
        from app.dao.script_dao import ScriptDAO
        
        # Try to parse as ID first, then as name
        script = None
        try:
            script_id = int(boot_task_data.custom_script)
            script = ScriptDAO.get_by_id(db, script_id)
        except ValueError:
            # Not an ID, try as name
            script = ScriptDAO.get_by_name(db, boot_task_data.custom_script)
        
        if not script:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Script '{boot_task_data.custom_script}' not found"
            )
        
        if not script.enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Script '{script.name}' is disabled"
            )
        
        # Get script content
        script_content = script.content
        
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
        
        # Get URLs - use temp_os_service to get correct URLs (serves from temp_os/{os_id}/files/)
        kernel_url = temp_os_service.get_kernel_url("debian-live", base_url)
        initrd_url = temp_os_service.get_initrd_url("debian-live", base_url)
        kernel_params = temp_os_service.get_kernel_params("debian-live", boot_task_data.kernel_params)
        
        # Add squashfs fetch URL (required for debian-live to boot properly)
        squashfs_url = temp_os_service.get_squashfs_url("debian-live", base_url)
        if squashfs_url and "fetch=" not in kernel_params:
            kernel_params = f"{kernel_params} fetch={squashfs_url}"
        
        # Add preseed URL
        if server_mac:
            preseed_url = f"{base_url}/api/servers/interaction/preseed?mac={server_mac}"
            kernel_params = f"{kernel_params} preseed/url={preseed_url}"
        
        # Set description if not provided
        if not boot_task_data.description:
            boot_task_data.description = f"Run script: {script.name}"
        
        # Set boot_type for custom script
        boot_type = BootType.TEMP_OS
    elif boot_task_data.boot_type.lower() == "temp_os":
        boot_type = BootType.TEMP_OS
        temp_os_id = boot_task_data.temp_os_id
    elif boot_type is None:  # Only set if not already set by template
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
        temp_os_id=temp_os_id if 'temp_os_id' in locals() else None,
        description=boot_task_data.description
    )
    
    # Generate download token for file access (one-time use)
    # Determine which files this boot task needs access to
    allowed_files = []
    allowed_patterns = []
    template_image_files = []
    
    # If template-based installation, check for template files in deploy/ directory
    if boot_task_data.template_id:
        template_service = get_template_service()
        template = template_service.get_template(boot_task_data.template_id)
        if template and template.template_dir:
            deploy_dir = template.template_dir / "deploy"
            if deploy_dir.exists() and deploy_dir.is_dir():
                # Look for windows.img and efi.img in deploy directory (path relative to template dir for URL/token)
                for img_file in ["windows.img", "efi.img"]:
                    img_path = deploy_dir / img_file
                    if img_path.exists():
                        template_image_files.append(img_file)
                        allowed_files.append(f"deploy/{img_file}")
        
        # Fallback: check for old disk_image format
        if template and template.disk_image and not template_image_files:
            disk_image_filename = template.disk_image.split("/")[-1]
            allowed_files.append(disk_image_filename)
        # For template installs, allow any file under the template (script fetches what it needs)
        if boot_task_data.template_id:
            allowed_patterns = ["*"]
    
    # Generate token (allow access to specific files or all files if none specified)
    download_token_service = get_download_token_service()
    download_token = download_token_service.generate_token(
        boot_task_id=boot_task.id,
        allowed_files=allowed_files if allowed_files else None,
        allowed_patterns=allowed_patterns if allowed_patterns else None,
        expires_in=900  # 15 minutes for image downloads
    )
    
    # Inject token and do variable replacement (single pass for all variables)
    if script_content:
        # Populate replacements if it's empty (for non-template boot tasks)
        if not replacements:
            # Get server info for replacements
            pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server_id)
            server_mac = pxe_port.mac_address if pxe_port else None
            os_disk = DiskDAO.get_os_disk(db, server_id)
            
            replacements = {
                "SERVER_IP": server.server_ip or "",
                "SERVER_MAC": server_mac or "",
                "SERVER_ID": str(server_id),
                "OS_BOOT_MODE": server.os_boot_mode.value if server.os_boot_mode else "uefi",
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
        
        # Add token to replacements if available
        if download_token:
            replacements["DOWNLOAD_TOKEN"] = download_token
            replacements["API_BASE_URL"] = base_url
            if boot_task_data.template_id:
                replacements["TEMPLATE_ID"] = boot_task_data.template_id
            
            # Add template file URLs if template has deploy directory with image files
            if boot_task_data.template_id and template_image_files:
                template_id = boot_task_data.template_id
                
                # Inject URLs for each image file found (file_path must be relative to template dir, e.g. deploy/windows.img)
                for img_file in template_image_files:
                    file_path = f"deploy/{img_file}"
                    if img_file == "windows.img":
                        windows_img_url = f"{base_url}/api/servers/interaction/template-files/{template_id}/{file_path}?token={download_token}"
                        replacements["WINDOWS_IMG_URL"] = windows_img_url
                        logger.info(f"Injected WINDOWS_IMG_URL for boot task {boot_task.id}")
                    elif img_file == "efi.img":
                        efi_img_url = f"{base_url}/api/servers/interaction/template-files/{template_id}/{file_path}?token={download_token}"
                        replacements["EFI_IMG_URL"] = efi_img_url
                        logger.info(f"Injected EFI_IMG_URL for boot task {boot_task.id}")
            
            # Fallback: old disk_image format
            elif boot_task_data.template_id:
                template_service = get_template_service()
                template = template_service.get_template(boot_task_data.template_id)
                if template and template.disk_image:
                    disk_image_filename = template.disk_image.split("/")[-1]
                    disk_image_url = f"{base_url}/api/servers/interaction/disk-images/{disk_image_filename}?token={download_token}"
                    replacements["DISK_IMAGE_URL"] = disk_image_url
                    logger.info(f"Injected DISK_IMAGE_URL for boot task {boot_task.id}: {disk_image_url[:50]}...")
        
        # Do single replacement pass for ALL variables (including tokens and disk info)
        # Replace both ${VAR} and ${VAR:-default} patterns
        for var_name, var_value in replacements.items():
            # Replace ${VAR} pattern
            script_content = script_content.replace(f"${{{var_name}}}", var_value)
            # Replace ${VAR:-default} pattern (bash default value syntax)
            import re
            pattern = re.compile(r'\$\{' + re.escape(var_name) + r':-[^}]*\}')
            script_content = pattern.sub(var_value, script_content)
            # Replace $VAR pattern (without braces)
            script_content = script_content.replace(f"${var_name}", var_value)
        
        # Update boot task with script containing all replacements
        boot_task.script_content = script_content
        db.commit()
        db.refresh(boot_task)
    
    # For Debian Live (squashfs), add script URL to kernel params if script exists
    if boot_task.temp_os_id == "debian-live" and boot_task.script_content:
        script_url = f"{base_url}/api/servers/interaction/scripts/{boot_task.id}"
        from urllib.parse import quote
        encoded_script_url = quote(script_url, safe=':/?=&')
        if "script_url=" not in boot_task.kernel_params:
            updated_kernel_params = f"{boot_task.kernel_params} script_url={encoded_script_url}"
            boot_task.kernel_params = updated_kernel_params
            db.commit()
            db.refresh(boot_task)
    
    # If this is a template-based installation, create InstallationTask and save credentials
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
        
        # Add INSTALLATION_TASK_ID to script replacements so install script can upload logs
        replacements["INSTALLATION_TASK_ID"] = str(installation_task.id)
        # Ensure template script gets TEMPLATE_ID and token for fetching firstboot.ps1 etc.
        if download_token:
            replacements["TEMPLATE_ID"] = boot_task_data.template_id
            replacements["DOWNLOAD_TOKEN"] = download_token
            replacements["API_BASE_URL"] = base_url

        # Re-apply replacements to script_content with the new INSTALLATION_TASK_ID (include ${VAR:-default})
        for var_name, var_value in replacements.items():
            script_content = script_content.replace(f"${{{var_name}}}", var_value)
            pattern = re.compile(r"\$\{" + re.escape(var_name) + r":-[^}]*\}")
            script_content = pattern.sub(var_value, script_content)
            script_content = script_content.replace(f"${var_name}", var_value)

        # Update boot task with the updated script content
        boot_task.script_content = script_content
        db.commit()
        db.refresh(boot_task)
        
        # Save credentials to server (passwords, etc.)
        if boot_task_data.template_parameters:
            credentials = server.credentials or {}
            # Store template parameters as credentials (especially passwords)
            credentials.update({
                'os_type': template.os_type if template else None,
                'template_id': boot_task_data.template_id,
                'last_updated': boot_task.created_at.isoformat() if boot_task.created_at else None
            })
            # Store individual parameters
            for param_name, param_value in boot_task_data.template_parameters.items():
                credentials[param_name] = param_value
            
            server.credentials = credentials
            db.commit()
            logger.info(f"Saved credentials for server {server_id}")
    
    logger.info(f"Created boot task {boot_task.id} for server {server_id}")
    if boot_task_data.template_id:
        log_server_activity_success(
            db,
            server_id=server_id,
            event_type=ServerActivityEventType.INSTALL,
            action="queue_template_install",
            source="admin_api",
            message=f"Queued template install '{boot_task_data.template_id}'",
            details={
                "boot_task_id": boot_task.id,
                "installation_task_id": installation_task.id if installation_task else None,
                "template_id": boot_task_data.template_id,
            },
        )
    elif boot_task_data.custom_script:
        log_server_activity_success(
            db,
            server_id=server_id,
            event_type=ServerActivityEventType.INSTALL,
            action="run_script",
            source="admin_api",
            message=f"Queued script '{boot_task_data.custom_script}'",
            details={"boot_task_id": boot_task.id},
        )
    
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
    token: Optional[str] = Query(None, description="One-time download token (optional for backward compatibility)"),
    db: Session = Depends(get_db)
):
    """
    Serve script content for a boot task.
    
    This endpoint is called by the Linux environment to fetch the script
    that should be executed.
    
    Token validation is optional for backward compatibility, but recommended.
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
    
    # Validate token if provided
    if token:
        download_token_service = get_download_token_service()
        token_data = download_token_service.validate_token(token, f"script-{task_id}.sh")
        
        if not token_data or token_data.get("boot_task_id") != task_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired download token"
            )
        
        # Mark token as used
        download_token_service.mark_token_used(token)
    
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
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    List all available scripts from database.
    
    Returns a list of scripts stored in the database.
    """
    from app.dao.script_dao import ScriptDAO
    
    scripts = ScriptDAO.get_all(db, enabled_only=True)
    
    return [
        {
            "id": script.id,
            "name": script.name,
            "description": script.description,
            "size_bytes": len(script.content.encode('utf-8')),
            "size_kb": round(len(script.content.encode('utf-8')) / 1024, 2),
            "user_executable": script.user_executable,
            "url": f"/api/servers/interaction/scripts/by-id/{script.id}"
        }
        for script in scripts
    ]


@router.get("/scripts/by-id/{script_id_or_name}")
async def get_script_by_id_or_name(
    script_id_or_name: str,
    token: Optional[str] = Query(None, description="One-time download token"),
    request: Request = None,
    db: Session = Depends(get_db)
):
    """
    Get script content by ID or name.
    
    Scripts are stored in the database and can be retrieved by ID or name.
    This endpoint is for retrieving script definitions, not boot task scripts.
    Use /scripts/{task_id} to get boot task script content.
    
    Requires authentication (admin) or a valid download token.
    """
    from app.dao.script_dao import ScriptDAO
    
    # Try to get auth token from request
    auth = None
    try:
        auth = await get_current_user(request, None, None, db)
    except HTTPException:
        pass  # Auth failed, will check token instead
    
    # Check authentication - either admin auth or valid token
    if not auth and not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required (admin token or download token)"
        )
    
    # Try to parse as ID first, then as name
    script = None
    try:
        script_id = int(script_id_or_name)
        script = ScriptDAO.get_by_id(db, script_id)
    except ValueError:
        # Not an ID, try as name
        script = ScriptDAO.get_by_name(db, script_id_or_name)
    
    if not script:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Script '{script_id_or_name}' not found"
        )
    
    logger.info(f"Serving script: {script.name} (ID: {script.id})")
    return PlainTextResponse(
        content=script.content,
        media_type="text/x-shellscript",
        headers={
            "Content-Disposition": f'inline; filename="{script.name}.sh"'
        }
    )


# ISO Serving Endpoints

@router.get("/isos")
async def list_isos(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
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
                "url": f"{_get_base_url_for_pxe_ip(db, None)}/api/servers/interaction/isos/{filename}"
            })
    
    return sorted(iso_files, key=lambda x: x["filename"])


@router.get("/isos/{filename}")
@router.head("/isos/{filename}")
async def get_iso(
    filename: str,
    request: Request,
    token: Optional[str] = Query(None, description="One-time download token"),
    db: Session = Depends(get_db)
):
    """
    Serve ISO image files.
    
    This endpoint serves ISO files for network booting.
    Place ISO files in the isos/ directory.
    Supports both GET and HEAD requests (HEAD for file size checks).
    
    Requires a valid one-time download token for security.
    """
    # Validate token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Download token required"
        )
    
    download_token_service = get_download_token_service()
    token_data = download_token_service.validate_token(token, filename)
    
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired download token"
        )
    
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
    
    # Mark token as used only if single-use (ISO boot reuses the same URL for initrd + chain/sanboot)
    if token_data.get("single_use", True):
        download_token_service.mark_token_used(token)
    
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
    
    logger.info(f"Serving ISO file: {filename} (boot_task: {token_data.get('boot_task_id')})")
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
            "requires_modloop": config.requires_modloop,
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
    token: Optional[str] = Query(None, description="One-time download token"),
    db: Session = Depends(get_db)
):
    """
    Serve disk image files (ISOs, disk images, etc.) from disk_images/ directory.
    
    This endpoint serves disk images for OS installation templates.
    Place disk image files in the disk_images/ directory.
    Supports both GET and HEAD requests (HEAD for file size checks).
    
    Requires a valid one-time download token for security.
    """
    # Validate token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Download token required"
        )
    
    download_token_service = get_download_token_service()
    token_data = download_token_service.validate_token(token, filename)
    
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired download token"
        )
    
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
    
    # Mark token as used (one-time use)
    download_token_service.mark_token_used(token)
    
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
    
    logger.info(f"Serving disk image file: {filename} (boot_task: {token_data.get('boot_task_id')})")
    return FileResponse(
        disk_image_path,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )


# Template Files Serving Endpoints

@router.get("/template-files/{template_id}/{file_path:path}")
@router.head("/template-files/{template_id}/{file_path:path}")
async def get_template_file(
    template_id: str,
    file_path: str,
    request: Request,
    token: Optional[str] = Query(None, description="One-time download token"),
    db: Session = Depends(get_db)
):
    """
    Serve any file under os_templates/{template_id}/ (template root).
    
    file_path is relative to the template directory, e.g. "deploy/windows.img",
    "deploy/efi.img", "firstboot.ps1", "user-login.ps1". Requires a valid
    download token for security.
    """
    # Validate token (token is validated against the same file_path for allowed_files)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Download token required"
        )
    
    download_token_service = get_download_token_service()
    token_data = download_token_service.validate_token(token, file_path)
    
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired download token"
        )
    
    # Get template directory
    template_service = get_template_service()
    template = template_service.get_template(template_id)
    
    if not template or not template.template_dir:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template '{template_id}' not found"
        )
    
    # Security: no directory traversal
    if ".." in file_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file path"
        )
    
    base_dir = template.template_dir
    full_path = base_dir / file_path
    
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template file '{file_path}' not found for template '{template_id}'"
        )
    
    # Security: ensure file is within the template directory (prevent traversal)
    try:
        full_path.resolve().relative_to(base_dir.resolve())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file path"
        )
    
    # Note: We don't mark token as used here since multiple files may be downloaded
    # Token will be explicitly terminated by the script when done
    
    # For HEAD requests, return headers only (no body)
    if request.method == "HEAD":
        file_size = full_path.stat().st_size
        return Response(
            status_code=200,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(file_size),
                "Content-Disposition": f'inline; filename="{file_path}"'
            }
        )
    
    logger.info(f"Serving template file: {template_id}/{file_path} (boot_task: {token_data.get('boot_task_id')})")
    return FileResponse(
        str(full_path),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{file_path}"'
        }
    )


@router.post("/download-token/{token}/terminate")
async def terminate_download_token(
    token: str,
    db: Session = Depends(get_db)
):
    """
    Explicitly terminate a download token.
    
    This endpoint allows installation scripts to explicitly terminate their download token
    when they're done downloading files, rather than waiting for expiration.
    """
    download_token_service = get_download_token_service()
    
    if download_token_service.revoke_token(token):
        return {"status": "success", "message": "Download token terminated"}
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token not found or already expired"
        )
