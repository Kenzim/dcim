"""Map billing/admin pydantic bodies onto ProvisionRequest."""
from __future__ import annotations

from typing import Any, Optional

from app.models.service import ProvisioningSource, ServiceType
from app.services.provisioning.errors import ProvisioningError
from app.services.provisioning.request import ProvisionRequest


def _as_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)


def from_billing_bare_metal(data, owner_user_id: int) -> ProvisionRequest:
    try:
        resolved = ServiceType((data.service_type or "bare_metal").lower())
    except ValueError as exc:
        raise ProvisioningError(
            "invalid_request",
            "Invalid service_type. Must be bare_metal, vm, or http_proxy",
        ) from exc
    if resolved == ServiceType.VM:
        raise ProvisioningError(
            "invalid_request",
            "VM services must be created via POST /billing/vm/services",
        )
    cfg = dict(data.service_config or {})
    return ProvisionRequest(
        name=data.name,
        service_type=resolved,
        owner_user_id=owner_user_id,
        product_code=data.product_code,
        external_service_id=data.external_service_id,
        description=data.description,
        provisioning_source=ProvisioningSource.BILLING,
        auto_provision=True,
        service_config=cfg,
        server_group_id=_as_int(cfg.get("server_group_id")),
        template_id=cfg.get("template_id") or None,
        template_parameters=cfg.get("template_parameters") or None,
        ip_count=_as_int(cfg.get("ip_count")),
        subnet_id=_as_int(cfg.get("subnet_id")),
        subnet_group_id=_as_int(cfg.get("subnet_group_id")),
        allocation_strategy=cfg.get("allocation_strategy"),
        leave_pending_on_placement_error=False,
    )


def from_billing_vm(body, owner_user_id: int) -> ProvisionRequest:
    return ProvisionRequest(
        name=body.name,
        service_type=ServiceType.VM,
        owner_user_id=owner_user_id,
        product_code=body.product_code,
        external_service_id=body.external_service_id,
        description=body.description,
        provisioning_source=ProvisioningSource.BILLING,
        auto_provision=bool(body.auto_provision),
        service_config=dict(body.service_config or {}),
        vm_template_id=body.vm_template_id,
        proxmox_cluster_id=body.proxmox_cluster_id,
        proxmox_node_name=body.proxmox_node_name,
        proxmox_vmid=body.proxmox_vmid,
        leave_pending_on_placement_error=False,
    )
