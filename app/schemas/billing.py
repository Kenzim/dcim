"""
Pydantic schemas for billing API requests and responses.
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


# Service schemas for billing API
class BillingBareMetalServiceCreate(BaseModel):
    """Create bare-metal or http_proxy service (uses RackFlow Server + service_bare_metal)."""
    name: str = Field(..., description="Service name")
    external_service_id: Optional[str] = Field(None, description="Service ID in external system")
    external_user_id: str = Field(..., description="User ID in external system")
    external_username: Optional[str] = Field(None, description="Username in external system")
    external_email: Optional[str] = Field(None, description="Email in external system")
    product_code: Optional[str] = Field(None, description="RackFlow product code")
    os_code: Optional[str] = Field(None, description="RackFlow OS profile code")
    service_type: Optional[str] = Field("bare_metal", description="bare_metal or http_proxy")
    # Server configuration
    server_name: Optional[str] = Field(None, description="Server name")
    server_ip: Optional[str] = Field(None, description="Server IP address")
    description: Optional[str] = Field(None, description="Service/Server description")
    cpu_count: int = Field(1, description="Number of CPUs")
    cpu_model: Optional[str] = Field(None, description="CPU model")
    ram_gb: Optional[int] = Field(None, description="RAM in GB")
    port_speed_mbps: Optional[int] = Field(None, description="Port speed in Mbps")
    location_id: Optional[int] = Field(None, description="Location ID")
    plugin_name: Optional[str] = Field(None, description="Plugin name (folder name on disk)")
    plugin_config: Dict[str, Any] = Field(default_factory=dict, description="Plugin configuration")
    os_boot_mode: Optional[str] = Field("uefi", description="OS boot mode (uefi/bios)")
    disks: List[Dict[str, Any]] = Field(default_factory=list, description="Disks configuration")
    network_ports: List[Dict[str, Any]] = Field(default_factory=list, description="Network ports configuration")
    service_config: Optional[Dict[str, Any]] = Field(None, description="Service-specific configuration")
    # Optional authoritative Proxmox placement (VM services); also mirrored on Server plugin_config when applicable
    proxmox_cluster_id: Optional[int] = Field(None, description="Deprecated for BM create; ignored")
    proxmox_node_name: Optional[str] = Field(None, description="Deprecated for BM create; ignored")
    proxmox_vmid: Optional[int] = Field(None, description="Deprecated for BM create; ignored")


class BillingVmServiceCreate(BaseModel):
    """Create VM service (no RackFlow Server row; uses service_vm + Proxmox placement)."""
    name: str = Field(..., description="Service name")
    external_service_id: Optional[str] = Field(None, description="Service ID in external system")
    external_user_id: str = Field(..., description="User ID in external system")
    external_username: Optional[str] = Field(None, description="Username in external system")
    external_email: Optional[str] = Field(None, description="Email in external system")
    product_code: Optional[str] = Field(None, description="RackFlow product code")
    vm_template_id: Optional[int] = Field(
        None,
        description="Catalog VM template id (preferred); linked to product. Strategy = template os_type (e.g. Linux - Cloudinit)",
    )
    os_code: Optional[str] = Field(
        None,
        description="Legacy: RackFlow OS profile code when not using vm_template_id",
    )
    description: Optional[str] = Field(None, description="Service description")
    service_config: Optional[Dict[str, Any]] = Field(None, description="Service-specific configuration")
    proxmox_cluster_id: Optional[int] = Field(None, description="Proxmox cluster id from RackFlow inventory")
    proxmox_node_name: Optional[str] = Field(None, description="Proxmox node name")
    proxmox_vmid: Optional[int] = Field(None, description="QEMU/KVM vmid")


class BillingServiceCreate(BillingBareMetalServiceCreate):
    """Deprecated: use POST /billing/bare-metal/services or POST /billing/vm/services."""

    pass


class BillingRegisterService(BaseModel):
    """Request schema for registering an existing server as a service (no provisioning)."""
    server_id: int = Field(..., description="RackFlow server ID (e.g. from server-by-ip lookup)")
    external_service_id: str = Field(..., description="External service ID (e.g. WHMCS service ID)")
    external_user_id: str = Field(..., description="External user/client ID")
    external_username: Optional[str] = Field(None, description="Username in external system")
    external_email: Optional[str] = Field(None, description="Email in external system")
    name: Optional[str] = Field(None, description="Service name; default service-{external_service_id}")


class BillingServiceResponse(BaseModel):
    """Response schema for service details"""
    id: int
    name: str
    external_service_id: Optional[str] = None
    service_type: Optional[str] = None
    product_code: Optional[str] = None
    os_code: Optional[str] = None
    vm_template_id: Optional[int] = Field(
        default=None,
        description="Catalog VM template id when service was created with vm_template_id",
    )
    server_id: Optional[int] = None
    external_user_id: Optional[int] = None
    provisioning_source: Optional[str] = Field(default="billing", description="billing or internal")
    proxmox_cluster_id: Optional[int] = None
    proxmox_node_name: Optional[str] = None
    proxmox_vmid: Optional[int] = None
    vm_ip_allocation_id: Optional[int] = Field(
        default=None,
        description="VM IP pool row id when a customer IP was allocated from the VM pool",
    )
    vm_ip_address: Optional[str] = Field(
        default=None,
        description="Customer / primary IP from the VM IP pool when allocated",
    )
    status: str
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    # Convenience fields for billing integrations (e.g. WHMCS)
    # These are populated when available and may be null for legacy calls.
    server_ip: Optional[str] = Field(
        default=None,
        description="Primary server IP address for this service (if available)",
    )
    credentials: Optional[Dict[str, Any]] = Field(
        default=None,
        description="OS / service credentials (e.g. admin username/password) when known",
    )
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class BillingServiceDetailResponse(BillingServiceResponse):
    """Extended service response with server details"""
    server: Optional[Dict[str, Any]] = None  # Bare metal only
    external_user: Optional[Dict[str, Any]] = None  # Absent for internal/test services


class PowerAction(BaseModel):
    """Request schema for power actions"""
    action: str = Field(..., description="Power action: on, off, reboot, reset")


class SuspendAction(BaseModel):
    """Request schema for suspend/unsuspend"""
    reason: Optional[str] = Field(None, description="Reason for suspend/unsuspend")


class ServerUsage(BaseModel):
    """Response schema for server usage/stats"""
    server_id: Optional[int] = None
    cpu_usage_percent: Optional[float] = None
    ram_usage_gb: Optional[float] = None
    ram_total_gb: Optional[int] = None
    disk_usage_gb: Optional[float] = None
    disk_total_gb: Optional[int] = None
    network_rx_bytes: Optional[int] = None
    network_tx_bytes: Optional[int] = None
    uptime_seconds: Optional[int] = None
    last_updated: Optional[datetime] = None


class ServiceActionRunScript(BaseModel):
    """Request schema for running a script on a service"""
    script_id: int = Field(..., description="Script ID to run")
    parameters: Optional[Dict[str, str]] = Field(default_factory=dict, description="Script parameters (for variable substitution)")


class ServiceActionReinstallOS(BaseModel):
    """Request schema for reinstalling OS on a service"""
    template_id: str = Field(..., description="OS template ID to install")
    template_parameters: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Template parameters (e.g., admin_password)")
