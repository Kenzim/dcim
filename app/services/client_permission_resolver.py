"""
Resolves the effective client permissions for a service by merging, from
least to most specific:

    1. Built-in defaults for the service's ``service_type``
    2. The product preset (``Product.permission_set_id``, via
       ``Service.product_code``)
    3. The owning user's preset (``User.permission_set_id``)
    4. The service's own preset (``Service.permission_set_id``)
    5. Sparse per-service overrides (``Service.permission_overrides``)

Each layer only overrides keys it explicitly sets; unset keys fall through
to the previous (less specific) layer. This lets an admin grant a client a
broad preset and then deny (or grant) one extra key on a single service
without having to duplicate the whole preset.

Admins/staff and billing lifecycle actions (create/suspend/unsuspend/
terminate) are not gated by these permissions — only the specific
client-facing actions listed in ``app.core.client_permissions``.
"""
from typing import Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.client_permissions import DEFAULT_PERMISSIONS_BY_SERVICE_TYPE
from app.models.service import Service
from app.models.permission_set import PermissionSet


def _merge(base: Dict[str, bool], overlay: Optional[Dict[str, bool]]) -> Dict[str, bool]:
    if not overlay:
        return base
    merged = dict(base)
    merged.update(overlay)
    return merged


def resolve_client_permissions(db: Session, service: Service) -> Dict[str, bool]:
    """Compute the effective client permission map for ``service``."""
    permissions: Dict[str, bool] = dict(
        DEFAULT_PERMISSIONS_BY_SERVICE_TYPE.get(service.service_type, {})
    )

    # Layer 2: product preset
    if service.product_code:
        from app.models.product_catalog import Product

        product = (
            db.query(Product).filter(Product.code == service.product_code).first()
        )
        if product and product.permission_set_id:
            preset = (
                db.query(PermissionSet)
                .filter(PermissionSet.id == product.permission_set_id)
                .first()
            )
            if preset:
                permissions = _merge(permissions, preset.permissions)

    # Layer 3: owning user's preset
    owner = service.owner_user
    if owner is not None and owner.permission_set_id:
        preset = (
            db.query(PermissionSet)
            .filter(PermissionSet.id == owner.permission_set_id)
            .first()
        )
        if preset:
            permissions = _merge(permissions, preset.permissions)

    # Layer 4: service-level preset
    if service.permission_set_id:
        preset = (
            db.query(PermissionSet)
            .filter(PermissionSet.id == service.permission_set_id)
            .first()
        )
        if preset:
            permissions = _merge(permissions, preset.permissions)

    # Layer 5: sparse per-service overrides (final word)
    permissions = _merge(permissions, service.permission_overrides)

    return permissions


def has_client_permission(db: Session, service: Service, key: str) -> bool:
    return bool(resolve_client_permissions(db, service).get(key, False))


def require_client_permission(db: Session, service: Service, key: str) -> None:
    """Raise 403 if ``service``'s owner is not granted permission ``key``."""
    if not has_client_permission(db, service, key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Client permission '{key}' is not granted for this service",
        )
