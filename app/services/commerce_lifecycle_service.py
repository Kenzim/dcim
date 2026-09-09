"""Retail service cancellation and upgrade quote orchestration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.commerce_account import BillingAccount
from app.models.reseller import InvoicePurpose, InvoiceStatus, ServiceBilling, ServiceBillingStatus
from app.models.service import Service, ServiceStatus
from app.models.storefront import FrontendProduct, PricePlan, PricePlanPricingModel
from app.services.invoice_service import InvoiceService
from app.services.service_lifecycle import ServiceLifecycle, ServiceLifecycleError

logger = logging.getLogger(__name__)

CancelWhen = Literal["end_of_cycle", "immediate"]


class CommerceLifecycleError(ValueError):
    pass


class CommerceLifecycleService:
    @staticmethod
    def _get_owned_service(
        db: Session, service_id: int, account: BillingAccount
    ) -> Service:
        service = db.get(Service, service_id)
        if service is None:
            raise CommerceLifecycleError("Service not found")
        if service.owner_user_id != account.user_id:
            raise CommerceLifecycleError("Service does not belong to this account")
        return service

    @staticmethod
    def _billing_for_service(
        db: Session, service_id: int
    ) -> Optional[ServiceBilling]:
        return db.execute(
            select(ServiceBilling).where(ServiceBilling.service_id == service_id)
        ).scalar_one_or_none()

    @staticmethod
    async def request_cancellation(
        db: Session,
        service_id: int,
        account: BillingAccount,
        *,
        when: CancelWhen = "end_of_cycle",
        reason: Optional[str] = None,
        force: bool = False,
    ) -> dict:
        if not force:
            CommerceLifecycleService._get_owned_service(db, service_id, account)
        else:
            service = db.get(Service, service_id)
            if service is None:
                raise CommerceLifecycleError("Service not found")

        billing = CommerceLifecycleService._billing_for_service(db, service_id)
        service = db.get(Service, service_id)
        note = (reason or "Client requested cancellation")[:500]
        now = datetime.now(timezone.utc)

        if billing is not None:
            if when == "end_of_cycle":
                billing.status = ServiceBillingStatus.CANCELLED
                billing.last_failure_code = "end_of_cycle_cancel"
                billing.last_failure_message = note
            else:
                billing.status = ServiceBillingStatus.CANCELLED
                billing.next_charge_at = None
                billing.next_retry_at = None
                billing.last_failure_code = "immediate_cancel"
                billing.last_failure_message = note

        if when == "immediate" and service is not None:
            if service.status not in {
                ServiceStatus.TERMINATED,
                ServiceStatus.SUSPENDED,
            }:
                lifecycle = ServiceLifecycle()
                try:
                    await lifecycle.suspend(db, service, reason="cancellation")
                except ServiceLifecycleError as exc:
                    logger.warning(
                        "Lifecycle suspend failed for service %s: %s",
                        service_id,
                        exc,
                    )
                service.status = ServiceStatus.TERMINATED
                service.terminated_at = now
                db.flush()

        return {
            "service_id": service_id,
            "when": when,
            "billing_status": billing.status.value if billing else None,
            "service_status": service.status.value if service else None,
        }

    @staticmethod
    def request_cancellation_sync(
        db: Session,
        service_id: int,
        account: BillingAccount,
        *,
        when: CancelWhen = "end_of_cycle",
        reason: Optional[str] = None,
        force: bool = False,
    ) -> dict:
        return asyncio.run(
            CommerceLifecycleService.request_cancellation(
                db,
                service_id,
                account,
                when=when,
                reason=reason,
                force=force,
            )
        )

    @staticmethod
    def create_upgrade_quote(
        db: Session,
        *,
        service_id: int,
        account: BillingAccount,
        frontend_product_id: int,
        price_plan_id: int,
        cycle_interval: Optional[str] = None,
    ) -> dict:
        service = CommerceLifecycleService._get_owned_service(
            db, service_id, account
        )
        product = db.execute(
            select(FrontendProduct)
            .options(joinedload(FrontendProduct.price_plans))
            .where(FrontendProduct.id == frontend_product_id)
        ).unique().scalar_one_or_none()
        if product is None or not product.enabled:
            raise CommerceLifecycleError("Target product is unavailable")

        plan = db.get(PricePlan, price_plan_id)
        if plan is None or plan.frontend_product_id != product.id or not plan.enabled:
            raise CommerceLifecycleError("Target price plan is unavailable")

        from app.services.checkout_service import CheckoutService

        setup_cents, recurring_cents = CheckoutService._resolve_cycle_price(
            plan, cycle_interval
        )
        current_billing = CommerceLifecycleService._billing_for_service(
            db, service.id
        )
        current_setup = int(current_billing.setup_price_cents if current_billing else 0)
        delta_setup = max(0, setup_cents - current_setup)

        if delta_setup <= 0:
            raise CommerceLifecycleError(
                "No positive upgrade charge; product change pending admin review"
            )

        invoice = InvoiceService.create(
            db,
            billing_account_id=account.id,
            purpose=InvoicePurpose.UPGRADE,
            amount_cents=delta_setup,
            currency=plan.currency,
            service_id=service.id,
            description=(
                f"Upgrade quote for service {service.name} "
                f"to {product.name} (pending admin approval)"
            ),
        )
        invoice.status = InvoiceStatus.OPEN

        return {
            "service_id": service.id,
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "amount_cents": delta_setup,
            "currency": plan.currency,
            "status": "pending_admin",
            "message": (
                "Upgrade invoice created. Product change applies after payment "
                "and admin approval."
            ),
            "target": {
                "frontend_product_id": frontend_product_id,
                "price_plan_id": price_plan_id,
                "cycle_interval": cycle_interval,
                "recurring_cents": recurring_cents,
                "pricing_model": plan.pricing_model.value,
            },
        }
