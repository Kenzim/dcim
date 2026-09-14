"""Single entry point for creating customer services."""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.dao.product_catalog_dao import ProductDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource, Service, ServiceType
from app.services.provisioning.bare_metal import create_bare_metal_service
from app.services.provisioning.errors import ProvisioningError
from app.services.provisioning.http_proxy import create_http_proxy_service
from app.services.provisioning.request import ProvisionRequest, ProvisioningActor
from app.services.provisioning.vm import create_vm_service
from app.services.service_product_snapshot import build_product_snapshot

logger = logging.getLogger(__name__)


def infer_provisioning_source(db: Session, owner_user_id: Optional[int]) -> ProvisioningSource:
    if owner_user_id is None:
        return ProvisioningSource.INTERNAL
    owner = UserDAO.get_by_id(db, owner_user_id)
    if owner is None:
        raise ProvisioningError("not_found", "Owner user not found")
    if owner.billing_integration_id:
        return ProvisioningSource.BILLING
    return ProvisioningSource.INTERNAL


def _default_vm_template_id(product) -> Optional[int]:
    rows = [
        m
        for m in (product.vm_template_mappings or [])
        if m.vm_template is not None and m.vm_template.enabled
    ]
    if not rows:
        return None
    rows.sort(key=lambda m: (m.vm_template.name or "").lower())
    return int(rows[0].vm_template_id)


def _apply_catalog_defaults(db: Session, req: ProvisionRequest) -> ProvisionRequest:
    if not req.product_code:
        return req
    product = ProductDAO.get_by_code(db, req.product_code)
    if not product:
        return req
    specs = {}
    family = product.family
    if family and family.defaults:
        specs.update(family.defaults)
    if product.overrides:
        specs.update(product.overrides)

    if req.service_type == ServiceType.BARE_METAL:
        if req.server_group_id is None and req.server_id is None:
            gid = specs.get("server_group_id")
            if gid not in (None, ""):
                req.server_group_id = int(gid)
    elif req.service_type == ServiceType.VM:
        if req.vm_template_id is None:
            req.vm_template_id = _default_vm_template_id(product)
        if req.proxmox_cluster_id is None and specs.get("proxmox_cluster_id") not in (None, ""):
            req.proxmox_cluster_id = int(specs["proxmox_cluster_id"])
    return req


def _build_snapshot(
    db: Session, req: ProvisionRequest
) -> tuple[dict[str, Any], Optional[str]]:
    if not req.product_code:
        return dict(req.extra_snapshot or {}), None
    try:
        snapshot, os_code = build_product_snapshot(
            db,
            req.product_code,
            None,
            req.service_type,
            vm_template_id=req.vm_template_id,
        )
    except ValueError as exc:
        raise ProvisioningError("invalid_request", str(exc)) from exc
    if req.extra_snapshot:
        snapshot = {**snapshot, **req.extra_snapshot}
    return snapshot, os_code


class ProvisioningService:
    @staticmethod
    def create(db: Session, req: ProvisionRequest, actor: ProvisioningActor) -> Service:
        if ServiceDAO.get_by_name(db, req.name):
            raise ProvisioningError("name_taken", "Service with this name already exists")

        req = _apply_catalog_defaults(db, req)
        snapshot, effective_os_code = _build_snapshot(db, req)

        logger.info(
            "Provisioning %s service '%s' via %s '%s'",
            req.service_type.value,
            req.name,
            actor.kind,
            actor.name,
        )

        if req.service_type == ServiceType.VM:
            return create_vm_service(db, req, actor, snapshot, effective_os_code)
        if req.service_type == ServiceType.HTTP_PROXY:
            return create_http_proxy_service(db, req, actor, snapshot)
        if req.service_type == ServiceType.BARE_METAL:
            return create_bare_metal_service(db, req, actor, snapshot)
        raise ProvisioningError(
            "invalid_request",
            f"Unsupported service_type '{req.service_type}'",
        )
