"""Bridge retail order fulfillment to recurring ServiceBilling rows."""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.commerce_account import BillingAccount
from app.models.commerce_order import OrderItem
from app.models.reseller import ServiceBilling, ServiceBillingStatus
from app.models.service import Service
from app.models.storefront import (
    PricePlan,
    PricePlanInterval,
    PricePlanPricingModel,
)

logger = logging.getLogger(__name__)


class CommerceRecurringService:
    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def next_due_at(now: datetime, interval: Optional[str]) -> datetime:
        """Advance ``now`` by one billing cycle interval."""
        current = CommerceRecurringService._utc(now)
        normalized = (interval or PricePlanInterval.MONTHLY.value).strip().lower()
        if normalized == PricePlanInterval.QUARTERLY.value:
            months = 3
        elif normalized == PricePlanInterval.SEMIANNUALLY.value:
            months = 6
        elif normalized == PricePlanInterval.ANNUALLY.value:
            months = 12
        else:
            months = 1
        month_index = current.month - 1 + months
        year = current.year + month_index // 12
        month = month_index % 12 + 1
        day = min(current.day, calendar.monthrange(year, month)[1])
        return current.replace(year=year, month=month, day=day)

    @staticmethod
    def ensure_for_fulfilled_item(
        db: Session,
        *,
        service: Service,
        item: OrderItem,
        billing_account: BillingAccount,
        plan: PricePlan,
        product_id: Optional[int] = None,
    ) -> Optional[ServiceBilling]:
        """Create ServiceBilling when a fulfilled order item uses a recurring plan."""
        if plan.pricing_model != PricePlanPricingModel.RECURRING:
            return None
        if not item.recurring_cents or int(item.recurring_cents) <= 0:
            return None

        existing = db.execute(
            select(ServiceBilling).where(ServiceBilling.service_id == service.id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        now = datetime.now(timezone.utc)
        next_charge = CommerceRecurringService.next_due_at(now, item.cycle_interval)
        if product_id is None and plan.frontend_product is not None:
            product_id = plan.frontend_product.product_id

        billing = ServiceBilling(
            service_id=service.id,
            reseller_id=billing_account.reseller_id,
            billing_account_id=billing_account.id,
            product_id=product_id,
            setup_price_cents=int(item.setup_cents or 0),
            monthly_price_cents=int(item.recurring_cents),
            currency=(item.currency or plan.currency or "USD").upper(),
            next_charge_at=next_charge,
            billing_anchor_day=next_charge.day,
            status=ServiceBillingStatus.ACTIVE,
            last_charged_at=now,
        )
        db.add(billing)
        db.flush()
        logger.info(
            "Created retail ServiceBilling for service %s (next charge %s)",
            service.id,
            next_charge.isoformat(),
        )
        return billing
