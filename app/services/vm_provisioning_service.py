from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.dao.product_catalog_dao import ProductDAO
from app.models.service import ServiceType
from app.services.service_product_snapshot import build_product_snapshot
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.vm_config_dao import ProductVMConfigDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.services.vm_os_strategy import VMProvisionRequest, get_vm_os_strategy_registry


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
        effective_specs = ProductVMConfigDAO.resolve_effective_config(db, product)
        if not effective_specs:
            effective_specs = dict(family.defaults or {})
            effective_specs.update(product.overrides or {})
        # Per-IP bridge overrides default network bridge from VM config.
        vm_ip_allocation_id = (context or {}).get("vm_ip_allocation_id")
        if vm_ip_allocation_id:
            allocation = VMIPAllocationDAO.get_by_id(db, int(vm_ip_allocation_id))
            if allocation and allocation.bridge_name:
                effective_specs["network_bridge"] = allocation.bridge_name

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
        result: list[dict] = []
        clusters = ProxmoxInventoryDAO.list_clusters(db)
        for cluster in clusters:
            nodes = cluster.nodes or []
            result.append(
                {
                    "cluster_id": cluster.id,
                    "cluster_name": cluster.name,
                    # Aliases for admin UIs that expect id/name (same as list endpoints elsewhere)
                    "id": cluster.id,
                    "name": cluster.name,
                    "api_url": cluster.api_url,
                    "vmid_min": cluster.vmid_min,
                    "vmid_max": cluster.vmid_max,
                    "node_count": len(nodes),
                    "template_count": sum(len(n.templates or []) for n in nodes),
                    "storage_count": sum(len(n.storages or []) for n in nodes),
                    "last_snapshots": [
                        n.capacity_snapshots[-1].created_at.isoformat()
                        for n in nodes
                        if n.capacity_snapshots
                    ],
                }
            )
        return result
