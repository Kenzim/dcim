"""Shared provision request + actor metadata."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.models.service import ProvisioningSource, ServiceType

# Storefront option values may only set these keys onto a ProvisionRequest.
ALLOWED_PROVISION_KEYS = frozenset(
    {
        "server_group_id",
        "server_id",
        "template_id",
        "vm_template_id",
        "proxmox_cluster_id",
        "proxmox_node_name",
        "proxmox_vmid",
        "ip_count",
        "subnet_id",
        "subnet_group_id",
        "allocation_strategy",
    }
)


@dataclass(frozen=True)
class ProvisioningActor:
    kind: str
    actor_id: int
    name: str
    source: str

    @property
    def details(self) -> dict[str, int]:
        return {f"{self.kind}_id": self.actor_id}

    @property
    def assigned_by(self) -> str:
        return f"{self.kind}:{self.name}"


@dataclass
class ProvisionRequest:
    name: str
    service_type: ServiceType
    owner_user_id: Optional[int] = None
    product_code: Optional[str] = None
    external_service_id: Optional[str] = None
    description: Optional[str] = None
    provisioning_source: ProvisioningSource = ProvisioningSource.INTERNAL
    auto_provision: bool = True
    service_config: dict[str, Any] = field(default_factory=dict)
    extra_snapshot: Optional[dict[str, Any]] = None
    permission_set_id: Optional[int] = None
    # Bare metal
    server_group_id: Optional[int] = None
    server_id: Optional[int] = None
    template_id: Optional[str] = None
    template_parameters: Optional[dict[str, Any]] = None
    # VM
    vm_template_id: Optional[int] = None
    proxmox_cluster_id: Optional[int] = None
    proxmox_node_name: Optional[str] = None
    proxmox_vmid: Optional[int] = None
    leave_pending_on_placement_error: bool = True
    # HTTP proxy
    ip_count: Optional[int] = None
    subnet_id: Optional[int] = None
    subnet_group_id: Optional[int] = None
    allocation_strategy: Optional[str] = None
