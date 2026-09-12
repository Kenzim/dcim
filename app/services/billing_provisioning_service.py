"""Shared entry point for billing and reseller service provisioning.

The mature provisioning implementation remains in ``app.api.billing`` for
phase-two compatibility, but both API surfaces enter it exclusively through
this facade.  The implementation receives a resolved owner and actor metadata;
it never needs a ``BillingIntegration`` object.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from app.models.service import Service
from app.schemas.billing import (
    BillingBareMetalServiceCreate,
    BillingVmServiceCreate,
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


async def provision_bare_metal_service(
    *,
    db: Session,
    service_data: BillingBareMetalServiceCreate,
    owner_user_id: int,
    actor: ProvisioningActor,
) -> Service:
    # Local import avoids an import cycle while the compatibility
    # implementation is incrementally moved out of the large billing router.
    from app.api.billing import _provision_bare_metal_service

    return _provision_bare_metal_service(
        service_data=service_data,
        owner_user_id=owner_user_id,
        actor=actor,
        db=db,
    )


async def provision_vm_service(
    *,
    db: Session,
    body: BillingVmServiceCreate,
    owner_user_id: int,
    actor: ProvisioningActor,
    background_tasks: BackgroundTasks,
) -> Service:
    from app.api.billing import _provision_vm_service

    return _provision_vm_service(
        body=body,
        owner_user_id=owner_user_id,
        actor=actor,
        background_tasks=background_tasks,
        db=db,
    )
