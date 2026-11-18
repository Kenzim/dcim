"""NFS storage service creation.

The service starts ``pending`` with its size recorded. It becomes ``active``
once it is linked to one of the owner's private networks (from the client
portal, the admin UI or MCP), which places it on a storage node.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.dao.service_dao import ServiceDAO
from app.dao.storage_dao import StorageDAO
from app.models.server_activity import ServerActivityEventType
from app.models.service import Service
from app.services.provisioning.errors import ProvisioningError
from app.services.provisioning.request import ProvisionRequest, ProvisioningActor
from app.services.server_activity_logger import log_server_activity_success
from app.services.storage_service import MAX_SIZE_GB, StorageError, StorageService, gb_to_bytes

logger = logging.getLogger(__name__)


def _size_gb(req: ProvisionRequest, product_snapshot: dict[str, Any]) -> int:
    specs = product_snapshot.get("effective_specs") or {}
    raw = req.size_gb if req.size_gb is not None else specs.get("size_gb")
    try:
        size = int(raw)
    except (TypeError, ValueError):
        raise ProvisioningError(
            "invalid_request", "nfs_storage needs size_gb (service_config or the product's size_gb)"
        ) from None
    if not 1 <= size <= MAX_SIZE_GB:
        raise ProvisioningError("invalid_request", f"size_gb must be between 1 and {MAX_SIZE_GB}")
    return size


def _location_id(db: Session, req: ProvisionRequest, product_snapshot: dict[str, Any]) -> Optional[int]:
    """The location chosen at order (or the product's), checked to offer storage. None: chosen at link."""
    specs = product_snapshot.get("effective_specs") or {}
    raw = req.location_id if req.location_id is not None else specs.get("location_id")
    if raw in (None, ""):
        return None
    try:
        location_id = int(raw)
    except (TypeError, ValueError):
        raise ProvisioningError("invalid_request", "location_id must be a location ID") from None
    try:
        return StorageService.check_location(db, location_id)
    except StorageError as exc:
        raise ProvisioningError("invalid_request", exc.message) from None


def create_nfs_storage_service(
    db: Session,
    req: ProvisionRequest,
    actor: ProvisioningActor,
    product_snapshot: dict[str, Any],
) -> Service:
    if req.owner_user_id is None:
        raise ProvisioningError("invalid_request", "nfs_storage services need an owner")
    size_gb = _size_gb(req, product_snapshot)
    location_id = _location_id(db, req, product_snapshot)
    try:
        service = ServiceDAO.create_nfs_storage(
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
        StorageDAO.create_tenant(
            db, service_id=service.id, size_bytes=gb_to_bytes(size_gb), location_id=location_id
        )
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
        message=f"Created NFS storage service '{service.name}' ({size_gb} GB), waiting for a private network",
        details={"service_id": service.id, **actor.details, "size_gb": size_gb, "location_id": location_id},
    )
    logger.info("Provisioned NFS storage service '%s' (ID: %s), %s GB", service.name, service.id, size_gb)
    return service
