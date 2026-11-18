"""Backup slot service creation.

A slot is active as soon as it exists: it needs no placement. It records the
largest backup it accepts and starts empty.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.dao.backup_slot_dao import BackupSlotDAO
from app.dao.service_dao import ServiceDAO
from app.models.server_activity import ServerActivityEventType
from app.models.service import Service
from app.services.backup_slot_service import MAX_SIZE_GB, gb_to_bytes
from app.services.provisioning.errors import ProvisioningError
from app.services.provisioning.request import ProvisionRequest, ProvisioningActor
from app.services.server_activity_logger import log_server_activity_success

logger = logging.getLogger(__name__)


def _size_gb(req: ProvisionRequest, product_snapshot: dict[str, Any]) -> int:
    specs = product_snapshot.get("effective_specs") or {}
    raw = req.size_gb if req.size_gb is not None else specs.get("size_gb")
    try:
        size = int(raw)
    except (TypeError, ValueError):
        raise ProvisioningError(
            "invalid_request", "backup_slot needs size_gb (service_config or the product's size_gb)"
        ) from None
    if not 1 <= size <= MAX_SIZE_GB:
        raise ProvisioningError("invalid_request", f"size_gb must be between 1 and {MAX_SIZE_GB}")
    return size


def create_backup_slot_service(
    db: Session,
    req: ProvisionRequest,
    actor: ProvisioningActor,
    product_snapshot: dict[str, Any],
) -> Service:
    if req.owner_user_id is None:
        raise ProvisioningError("invalid_request", "backup_slot services need an owner")
    size_gb = _size_gb(req, product_snapshot)
    try:
        service = ServiceDAO.create_backup_slot(
            db,
            name=req.name,
            owner_user_id=req.owner_user_id,
            external_service_id=req.external_service_id,
            description=req.description,
            config=req.service_config or {},
            product_code=req.product_code,
            product_snapshot=product_snapshot,
            provisioning_source=req.provisioning_source,
        )
        if req.permission_set_id is not None:
            service.permission_set_id = req.permission_set_id
        BackupSlotDAO.create_slot(db, service_id=service.id, size_bytes=gb_to_bytes(size_gb))
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(service)
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create",
        source=actor.source,
        message=f"Created backup slot '{service.name}' ({size_gb} GB)",
        details={"service_id": service.id, **actor.details, "size_gb": size_gb},
    )
    logger.info("Provisioned backup slot service '%s' (ID: %s), %s GB", service.name, service.id, size_gb)
    return service
