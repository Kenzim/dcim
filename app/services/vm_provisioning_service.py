from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.dao.product_catalog_dao import ProductDAO
from app.models.service import ServiceType
from app.services.service_product_snapshot import build_product_snapshot
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.vm_config_dao import ProductVMConfigDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.services.vm_os_strategy import VMProvisionRequest, get_vm_os_strategy_registry


def _normalize_vm_disk_and_clone(normalized: Dict[str, Any]) -> None:
    if normalized.get("disk_gb") in (None, ""):
        for alias in ("storage_gb", "disk_size_gb"):
            if normalized.get(alias) not in (None, ""):
                normalized["disk_gb"] = normalized[alias]
                break
    if "full_clone" not in normalized or normalized.get("full_clone") in (None, ""):
        normalized["full_clone"] = False
    else:
        normalized["full_clone"] = bool(normalized["full_clone"])


def _normalize_vm_specs(specs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Catalog VM config and legacy family/product defaults use several historical
    key names for the same sizing values (``cpu_cores`` / ``cpu_count`` for core
    count, ``ram_mb`` for memory), but the Proxmox executor
    (``app/services/vm_strategy_executor.py``) reads canonical ``cores`` /
    ``memory_mb`` keys when applying sizing to the cloned VM.

    Populate the canonical keys from whichever alias is present without
    removing the originals, so existing catalog fields/tests that read the
    alias names keep working while provisioning actually applies the
    configured values.
    """
    normalized = dict(specs)
    if normalized.get("cores") in (None, ""):
        for alias in ("cpu_cores", "cpu_count"):
            if normalized.get(alias) not in (None, ""):
                normalized["cores"] = normalized[alias]
                break
    if normalized.get("memory_mb") in (None, ""):
        if normalized.get("ram_mb") not in (None, ""):
            normalized["memory_mb"] = normalized["ram_mb"]
    _normalize_vm_disk_and_clone(normalized)
    return normalized


def _resolve_effective_specs(
    db: Session,
    product,
    context: Dict[str, Any] | None,
) -> Dict[str, Any]:
    family = product.family
    effective_specs = ProductVMConfigDAO.resolve_effective_config(db, product)
    if not effective_specs:
        effective_specs = dict(family.defaults or {})
        effective_specs.update(product.overrides or {})
    effective_specs = _normalize_vm_specs(effective_specs)
    vm_ip_allocation_id = (context or {}).get("vm_ip_allocation_id")
    if vm_ip_allocation_id:
        allocation = VMIPAllocationDAO.get_by_id(db, int(vm_ip_allocation_id))
        if allocation and allocation.bridge_name:
            effective_specs["network_bridge"] = allocation.bridge_name
    return effective_specs


class VMProvisioningService:
    """Orchestrates VM provisioning flow with strategy framework stubs."""

    @staticmethod
    def plan_provisioning(
        db: Session,
        service_id: int,
        product_code: str,
        os_code: Optional[str] = None,
        vm_template_id: Optional[int] = None,
        context: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        snapshot, effective_os_code = build_product_snapshot(
            db,
            product_code,
            os_code,
            ServiceType.VM,
            vm_template_id=vm_template_id,
        )
        if not effective_os_code:
            raise ValueError(
                "Either os_code or vm_template_id (template os_type defines strategy) is required"
            )
        op = snapshot.get("os_profile") or {}
        strategy_name = op.get("strategy_name")
        strategy_config = op.get("strategy_config") or {}
        if not op:
            raise ValueError("Product snapshot is missing os_profile for provisioning")

        product = ProductDAO.get_by_code(db, product_code)
        if not product:
            raise ValueError(f"Unknown product_code '{product_code}'")

        family = product.family
        effective_specs = _resolve_effective_specs(db, product, context)

        strategy = get_vm_os_strategy_registry().resolve(strategy_name)
        request = VMProvisionRequest(
            service_id=service_id,
            product_code=product_code,
            os_code=effective_os_code,
            specs=effective_specs,
            context=context or {},
        )
        plan = strategy.build_plan(request, strategy_config)
        return {
            "service_id": service_id,
            "product_code": product_code,
            "os_code": effective_os_code,
            "vm_template": snapshot.get("vm_template"),
            "family_code": family.code,
            "backend": family.provisioning_backend,
            "effective_specs": effective_specs,
            "strategy_plan": plan.payload,
            "strategy_name": plan.strategy_name,
        }

    @staticmethod
    def get_cluster_capacity_summary(db: Session) -> list[dict]:
        # Delegates to a handful of aggregate queries (DAO) instead of hydrating
        # every node's full templates/storages/capacity_snapshots collections,
        # which grow unbounded across syncs and made this list slow to load.
        return ProxmoxInventoryDAO.get_cluster_capacity_summary(db)
