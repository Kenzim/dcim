"""Shared product snapshot building for Service rows (billing + admin)."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.dao.product_catalog_dao import ProductDAO, VMTemplateDAO
from app.models.service import ServiceType
from app.dao.vm_config_dao import ProductVMConfigDAO
from app.services.vm_install_type_strategy import resolve_vm_template_strategy


def build_product_snapshot(
    db: Session,
    product_code: str,
    os_code: Optional[str],
    resolved_service_type: ServiceType,
    vm_template_id: Optional[int] = None,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Validate product/template against service_type and return (product_snapshot, effective_os_code).

    For VM services with ``vm_template_id``, strategy comes only from ``VMTemplate.os_type``.
    ``effective_os_code`` is the synthetic billing code for that strategy (stored on ``Service.os_code``).

    ``os_code`` is accepted only as a consistency check against the template's billing code.

    Raises ValueError with a human-readable message on validation failure.
    """
    product = ProductDAO.get_by_code(db, product_code)
    if not product:
        raise ValueError(f"Unknown product_code '{product_code}'")

    family = product.family
    if family.service_type != resolved_service_type.value:
        raise ValueError(
            f"Product '{product.code}' belongs to service_type '{family.service_type}', "
            f"not '{resolved_service_type.value}'"
        )

    effective_specs = ProductVMConfigDAO.resolve_effective_config(db, product)
    if not effective_specs:
        effective_specs = {**(family.defaults or {}), **(product.overrides or {})}

    snapshot: Dict[str, Any] = {
        "family": {
            "id": family.id,
            "code": family.code,
            "name": family.name,
            "service_type": family.service_type,
            "provisioning_backend": family.provisioning_backend,
        },
        "product": {
            "id": product.id,
            "code": product.code,
            "name": product.name,
        },
        "effective_specs": effective_specs,
    }

    effective_os_code: Optional[str] = None

    if resolved_service_type == ServiceType.VM and vm_template_id is not None:
        vm_template_row = VMTemplateDAO.get_by_id(db, vm_template_id)
        if not vm_template_row:
            raise ValueError(f"Unknown vm_template_id {vm_template_id}")
        if not ProductDAO.product_has_vm_template(db, product, vm_template_id):
            raise ValueError(
                f"vm_template_id {vm_template_id} is not linked to product '{product.code}' "
                "(assign VM templates on the product in the catalog)"
            )
        from app.services.vm_install_type_strategy import merge_strategy_options

        spec = resolve_vm_template_strategy(vm_template_row.os_type)
        effective_os_code = spec["billing_os_code"]
        if os_code and os_code != effective_os_code:
            raise ValueError(
                f"os_code '{os_code}' does not match strategy billing code '{effective_os_code}' "
                f"for vm_template_id {vm_template_id}"
            )
        merged_cfg = merge_strategy_options(
            vm_template_row.os_type,
            getattr(vm_template_row, "strategy_options", None) or {},
        )
        snapshot["vm_template"] = {
            "id": vm_template_row.id,
            "name": vm_template_row.name,
            "os_type": vm_template_row.os_type,
            "proxmox_template_name": vm_template_row.proxmox_template_name,
            "strategy_options": dict(getattr(vm_template_row, "strategy_options", None) or {}),
        }
        snapshot["os_profile"] = {
            "id": None,
            "code": spec["billing_os_code"],
            "name": vm_template_row.os_type,
            "family": "vm_template_strategy",
            "strategy_name": spec["strategy_name"],
            "strategy_config": merged_cfg,
            "source": "vm_template_os_type",
        }
    elif os_code:
        raise ValueError(
            "os_code is no longer supported; provision VMs with vm_template_id "
            "(template os_type defines the strategy)"
        )

    return snapshot, effective_os_code
