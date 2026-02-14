"""
Pydantic schemas for billing API requests and responses.
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


# Service schemas for billing API
class BillingServiceCreate(BaseModel):
    """Request schema for creating a service via billing API"""
    name: str = Field(..., description="Service name")
    external_service_id: Optional[str] = Field(None, description="Service ID in external system")
    external_user_id: str = Field(..., description="User ID in external system")
    external_username: Optional[str] = Field(None, description="Username in external system")
    external_email: Optional[str] = Field(None, description="Email in external system")
    # Server configuration
    server_name: str = Field(..., description="Server name")
    server_ip: str = Field(..., description="Server IP address")
    description: Optional[str] = Field(None, description="Service/Server description")
    cpu_count: int = Field(1, description="Number of CPUs")
    cpu_model: Optional[str] = Field(None, description="CPU model")
    ram_gb: Optional[int] = Field(None, description="RAM in GB")
    port_speed_mbps: Optional[int] = Field(None, description="Port speed in Mbps")
    location_id: int = Field(..., description="Location ID")
    plugin_name: str = Field(..., description="Plugin name (folder name on disk)")
    plugin_config: Dict[str, Any] = Field(..., description="Plugin configuration")
    os_boot_mode: Optional[str] = Field("uefi", description="OS boot mode (uefi/bios)")
    disks: List[Dict[str, Any]] = Field(default_factory=list, description="Disks configuration")
    network_ports: List[Dict[str, Any]] = Field(default_factory=list, description="Network ports configuration")
    service_config: Optional[Dict[str, Any]] = Field(None, description="Service-specific configuration")


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
    external_service_id: Optional[str]
    server_id: int
    external_user_id: int
    status: str
    description: Optional[str]
    config: Optional[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class BillingServiceDetailResponse(BillingServiceResponse):
    """Extended service response with server details"""
    server: Dict[str, Any]  # Server details
    external_user: Dict[str, Any]  # External user details


class PowerAction(BaseModel):
    """Request schema for power actions"""
    action: str = Field(..., description="Power action: on, off, reboot, reset")


class SuspendAction(BaseModel):
    """Request schema for suspend/unsuspend"""
    reason: Optional[str] = Field(None, description="Reason for suspend/unsuspend")


class ServerUsage(BaseModel):
    """Response schema for server usage/stats"""
    server_id: int
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
