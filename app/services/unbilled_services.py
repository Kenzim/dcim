"""Read-only check for services that should have a billing record and have none.

A reseller deploy commits the service before its ``ServiceBilling``, so a crash
or a failed clean-up leaves a live service nobody bills. One function answers
the question for the staff listing and, later, for an alert detector.

Suspects (billing-sourced, not terminated, older than the grace period, with no
``ServiceBilling`` row of any status):

* ``reseller_client``: the owner is a reseller's client.
* ``retail_order``: the owner is a retail client and the service was created
  from an order item with a recurring price.

Everything else may legitimately have no billing: staff-made (internal) and
owner-less services, WHMCS-owned services (billed outside Rackflow), and
one-off retail services. The query takes no locks and writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.commerce_order import OrderItem
from app.models.reseller import ServiceBilling
from app.models.service import ProvisioningSource, Service, ServiceStatus
from app.models.user import User

KIND_RESELLER_CLIENT = "reseller_client"
KIND_RETAIL_ORDER = "retail_order"

DEFAULT_MIN_AGE_MINUTES = 30
DEFAULT_LIMIT = 200


@dataclass(frozen=True)
class UnbilledService:
    """Ids and names only: no config, credentials or product snapshot."""

    kind: str
    service_id: int
    name: str
    service_type: str
    status: str
    owner_user_id: Optional[int]
    reseller_id: Optional[int]
    external_service_id: Optional[str]
    created_at: Optional[datetime]


@dataclass(frozen=True)
class UnbilledServices:
    items: list[UnbilledService]
    total: int
    omitted: int


def _enum_value(value) -> str:
    return getattr(value, "value", value)


def find_unbilled_services(
    db: Session,
    *,
    now: Optional[datetime] = None,
    min_age_minutes: int = DEFAULT_MIN_AGE_MINUTES,
    limit: int = DEFAULT_LIMIT,
) -> UnbilledServices:
    """List suspects ordered by service id; ``now`` is naive UTC (default: the clock)."""
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(minutes=min_age_minutes)

    has_billing = exists().where(ServiceBilling.service_id == Service.id)
    has_recurring_item = exists().where(
        OrderItem.service_id == Service.id, OrderItem.recurring_cents > 0
    )
    suspect = and_(
        Service.provisioning_source == ProvisioningSource.BILLING,
        Service.status != ServiceStatus.TERMINATED,
        Service.created_at <= cutoff,
        User.billing_integration_id.is_(None),
        ~has_billing,
        or_(
            User.reseller_id.is_not(None),
            and_(User.reseller_id.is_(None), has_recurring_item),
        ),
    )
    owned = Service.owner_user_id == User.id

    total = db.execute(
        select(func.count()).select_from(Service).join(User, owned).where(suspect)
    ).scalar_one()
    rows = db.execute(
        select(Service, User.reseller_id)
        .join(User, owned)
        .where(suspect)
        .order_by(Service.id)
        .limit(max(int(limit), 0))
    ).all()

    items = [
        UnbilledService(
            kind=KIND_RESELLER_CLIENT if reseller_id is not None else KIND_RETAIL_ORDER,
            service_id=service.id,
            name=service.name,
            service_type=_enum_value(service.service_type),
            status=_enum_value(service.status),
            owner_user_id=service.owner_user_id,
            reseller_id=reseller_id,
            external_service_id=service.external_service_id,
            created_at=service.created_at,
        )
        for service, reseller_id in rows
    ]
    return UnbilledServices(items=items, total=total, omitted=max(total - len(items), 0))
