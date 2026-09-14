"""
Pydantic schemas for billing API requests and responses.
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


# Service schemas for billing API
class BillingBareMetalServiceCreate(BaseModel):
    """Create bare-metal or http_proxy service.

    Bare metal requires ``service_config.server_group_id`` (pooled rack server).
    HTTP proxy assigns IPs from IPAM; no rack Server is created.
    """
    name: str = Field(..., description="Service name")
    external_service_id: Optional[str] = Field(None, description="Service ID in external system")
    external_user_id: str = Field(..., description="User ID in external system")
    external_username: Optional[str] = Field(None, description="Username in external system")
    external_email: Optional[str] = Field(None, description="Email in external system")
    product_code: Optional[str] = Field(None, description="RackFlow product code")
    service_type: Optional[str] = Field("bare_metal", description="bare_metal or http_proxy")
    description: Optional[str] = Field(None, description="Service description")
    service_config: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Type-specific config. Bare metal: server_group_id (required), "
            "template_id, template_parameters. Proxy: ip_count, subnet_id, "
            "subnet_group_id, allocation_strategy."
        ),
    )


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
    description: Optional[str] = Field(None, description="Service description")
    service_config: Optional[Dict[str, Any]] = Field(None, description="Service-specific configuration")
    proxmox_cluster_id: Optional[int] = Field(None, description="Proxmox cluster id from RackFlow inventory")
    proxmox_node_name: Optional[str] = Field(None, description="Proxmox node name")
    proxmox_vmid: Optional[int] = Field(None, description="QEMU/KVM vmid")
    auto_provision: bool = Field(
        default=True,
        description=(
            "When true (default), RackFlow resolves placement (auto-places from inventory if node omitted), "
            "reserves a VMID, and provisions the guest in the background. Poll GET /billing/services/{id} "
            "(vm_guest_state / config.vm_provision.status). Set false to create a pending service only."
        ),
    )


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


class BillingLinkService(BaseModel):
    """Bind a RackFlow service to an external (e.g. WHMCS) line item."""
    external_service_id: str = Field(..., description="External service ID (e.g. WHMCS hosting id)")
    external_user_id: Optional[str] = Field(
        None,
        description="External user/client ID; when set, finds or creates the external user and assigns ownership",
    )
    external_username: Optional[str] = Field(None, description="Username in external system")
    external_email: Optional[str] = Field(None, description="Email in external system")


class BillingAdoptVmService(BaseModel):
    """Adopt an existing Proxmox guest as a billing VM service and bind it externally."""
    external_service_id: str = Field(..., description="External service ID (e.g. WHMCS hosting id)")
    external_user_id: str = Field(..., description="External user/client ID")
    external_username: Optional[str] = Field(None, description="Username in external system")
    external_email: Optional[str] = Field(None, description="Email in external system")
    proxmox_cluster_id: int
    proxmox_node_name: str
    proxmox_vmid: int
    name: Optional[str] = Field(None, description="Service name; defaults to Proxmox guest name")
    product_code: Optional[str] = None


class BillingVmPlacementUpdate(BaseModel):
    """Update Proxmox placement for a VM billing service."""
    proxmox_cluster_id: int
    proxmox_node_name: str
    proxmox_vmid: Optional[int] = Field(
        None,
        description="Requested VMID; omitted/null lets the allocator pick the next free id",
    )
    adopt_existing: bool = Field(
        False,
        description=(
            "When true, bind to an existing Proxmox guest VMID (skip nextid availability check "
            "and allow VMIDs outside the cluster auto-allocation range)."
        ),
    )


class BillingServiceLookupItem(BaseModel):
    """Lookup row for WHMCS admin link search (RackFlow service or unmanaged Proxmox guest)."""
    id: Optional[int] = None
    name: str
    external_service_id: Optional[str] = None
    service_type: Optional[str] = None
    status: Optional[str] = None
    proxmox_cluster_id: Optional[int] = None
    proxmox_node_name: Optional[str] = None
    proxmox_vmid: Optional[int] = None
    server_ip: Optional[str] = None
    server_name: Optional[str] = None
    source: str = Field("rackflow", description="rackflow or proxmox")
    proxmox_status: Optional[str] = None


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
    vm_guest_state: Optional[str] = Field(
        default=None,
        description="VM guest lifecycle state (unprovisioned/provisioning/running/stopped/error/destroyed)",
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
    proxy_assignments: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="For http_proxy services: assigned IP(s) + credentials + ready-to-use proxy URLs",
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
