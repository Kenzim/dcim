"""Retail service cancellation and upgrade quote orchestration."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Optional, TypeVar

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


class ServiceTerminationError(RuntimeError):
    """The service could not be powered off, so it was not marked terminated."""


_T = TypeVar("_T")


def run_lifecycle_sync(make: Callable[[], Awaitable[_T]]) -> _T:
    """Run a lifecycle coroutine from synchronous code and return its result.

    Inside a running event loop the coroutine runs on a loop of its own in a
    helper thread while this thread waits, so the session is still used by one
    thread at a time.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make())  # type: ignore[arg-type]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(make())).result()  # type: ignore[arg-type]


async def terminate_service(
    db: Session,
    service: Service,
    *,
    lifecycle: Optional[ServiceLifecycle] = None,
    now: Optional[datetime] = None,
    reason: str = "cancellation",
) -> bool:
    """Power the service off and mark it terminated. Returns False if it already was.

    Raises ``ServiceTerminationError`` when the power-off fails: the service is
    left as it is, the failure is audited as ``billing.service_termination_failed``,
    and the caller must keep the billing due so a later pass retries.
    """
    if service.status == ServiceStatus.TERMINATED:
        return False
    if service.status != ServiceStatus.SUSPENDED:
        try:
            await (lifecycle or ServiceLifecycle()).suspend(db, service, reason=reason)
        except ServiceLifecycleError as exc:
            logger.warning(
                "Lifecycle suspend failed for service %s: %s", service.id, exc
            )
            from app.services.audit_service import AuditService

            AuditService.log(
                db,
                actor_user_id=None,
                action="billing.service_termination_failed",
                resource_type="service",
                resource_id=str(service.id),
                metadata={"reason": reason, "error": str(exc)[:500]},
            )
            raise ServiceTerminationError(str(exc)) from exc
    service.status = ServiceStatus.TERMINATED
    service.terminated_at = now or datetime.now(timezone.utc)
    db.flush()
    return True


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
        """Lock and re-read the service's billing row (first in the billing lock order)."""
        from app.services.billing_locks import BillingLocks

        billing_id = db.scalar(
            select(ServiceBilling.id).where(ServiceBilling.service_id == service_id)
        )
        if billing_id is None:
            return None
        return BillingLocks.service_billing(db, int(billing_id))

    @staticmethod
    async def request_cancellation(
        db: Session,
        service_id: int,
        account: Optional[BillingAccount],
        *,
        when: CancelWhen = "end_of_cycle",
        reason: Optional[str] = None,
        force: bool = False,
        lifecycle: Optional[Any] = None,
    ) -> dict:
        """Cancel a service at the end of its period or at once.

        Without ``force`` the service must belong to ``account``; callers that
        checked ownership themselves (``CancellationService.request``) pass
        ``force=True`` and may pass no account.
        """
        if not force and account is None:
            raise CommerceLifecycleError("Service does not belong to this account")
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

        termination_pending = False
        if billing is not None:
            if when == "end_of_cycle":
                if billing.status == ServiceBillingStatus.CANCELLED:
                    pass
                elif billing.next_charge_at is not None:
                    billing.cancel_at = billing.cancel_at or billing.next_charge_at
                    billing.last_failure_message = note
                else:
                    billing.status = ServiceBillingStatus.CANCELLED
                    billing.last_failure_code = "end_of_cycle_cancel"
                    billing.last_failure_message = note

        if (
            when == "immediate"
            and service is not None
            and service.status not in {
                ServiceStatus.TERMINATED,
                ServiceStatus.SUSPENDED,
            }
        ):
            try:
                await terminate_service(db, service, lifecycle=lifecycle, now=now)
            except ServiceTerminationError:
                termination_pending = True

        if billing is not None and when == "immediate":
            if termination_pending and billing.status != ServiceBillingStatus.CANCELLED:
                # The service is still running: end the billing at once through
                # the recurring worker, which retries the termination and only
                # then cancels the billing. Nothing is charged meanwhile.
                billing.cancel_at = now
                billing.last_failure_code = "immediate_cancel_pending"
                billing.last_failure_message = note
            else:
                billing.status = ServiceBillingStatus.CANCELLED
                billing.next_charge_at = None
                billing.next_retry_at = None
                billing.cancel_at = None
                billing.last_failure_code = "immediate_cancel"
                billing.last_failure_message = note

        return {
            "service_id": service_id,
            "when": when,
            "billing_status": billing.status.value if billing else None,
            "service_status": service.status.value if service else None,
            "termination_pending": termination_pending,
            "cancel_at": (
                billing.cancel_at.isoformat()
                if billing is not None and billing.cancel_at is not None
                else None
            ),
        }

    @staticmethod
    def request_cancellation_sync(
        db: Session,
        service_id: int,
        account: Optional[BillingAccount],
        *,
        when: CancelWhen = "end_of_cycle",
        reason: Optional[str] = None,
        force: bool = False,
        lifecycle: Optional[Any] = None,
    ) -> dict:
        return run_lifecycle_sync(
            lambda: CommerceLifecycleService.request_cancellation(
                db,
                service_id,
                account,
                when=when,
                reason=reason,
                force=force,
                lifecycle=lifecycle,
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
