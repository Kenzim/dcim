"""Account funders for recurring billing cycles, and the retail renewal steps.

The recurring billing worker asks an ``AccountFunder`` to pay a cycle invoice.
Reseller rows use ``ResellerFunder``, which is the existing orchestrator path
(credit, then saved methods) unchanged. Retail rows use ``RetailInvoiceFunder``:
until saved retail payment methods exist, the cycle invoice stays open, is
emailed to the account owner, and the client pays it in the client area.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    InvoicePurpose,
    ServiceBilling,
    ServiceBillingStatus,
)
from app.services.billing.intervals import advance_months
from app.services.billing_locks import BillingLocks

logger = logging.getLogger(__name__)

RETAIL_RECHECK_INTERVAL = timedelta(days=1)


@dataclass(frozen=True)
class FundingOutcome:
    funded: bool
    pending_action: bool = False
    awaiting_client: bool = False

    def __bool__(self) -> bool:
        return self.funded


class AccountFunder(Protocol):
    def fund(
        self, db: Session, billing: ServiceBilling, cycle: BillingCycle
    ):  # returns an object with ``funded`` and ``pending_action``
        ...


class ResellerFunder:
    """Pay a reseller cycle invoice from credit, then saved methods."""

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    def fund(self, db: Session, billing: ServiceBilling, cycle: BillingCycle):
        return self.orchestrator.fund_invoice(db, billing.reseller, cycle.invoice)


class RetailInvoiceFunder:
    """Leave a retail cycle invoice open for the client and email it."""

    def fund(
        self, db: Session, billing: ServiceBilling, cycle: BillingCycle
    ) -> FundingOutcome:
        email_cycle_invoice(db, billing, cycle.invoice)
        return FundingOutcome(funded=False, awaiting_client=True)


def is_retail(billing: ServiceBilling) -> bool:
    return billing.reseller_id is None


def billing_interval_months(billing: ServiceBilling) -> int:
    return int(billing.interval_months or 1)


def email_cycle_invoice(db: Session, billing: ServiceBilling, invoice: Invoice) -> None:
    from app.models.commerce_account import BillingAccount
    from app.models.user import User
    from app.services.email_message_service import EmailEvent, EmailMessageService

    account = (
        db.get(BillingAccount, billing.billing_account_id)
        if billing.billing_account_id is not None
        else None
    )
    user = db.get(User, account.user_id) if account is not None and account.user_id else None
    if user is None or not user.email:
        logger.warning(
            "Retail cycle invoice %s has no recipient; it stays open in the client area",
            invoice.id,
        )
        return
    EmailMessageService.enqueue(
        db,
        to_address=user.email,
        event=EmailEvent.INVOICE_CREATED,
        idempotency_key=f"invoice:{invoice.id}:created",
        data={
            "invoice_number": invoice.invoice_number,
            "invoice_id": invoice.id,
            "amount_cents": int(invoice.amount_cents),
            "currency": invoice.currency,
        },
        billing_account_id=billing.billing_account_id,
        user_id=user.id,
        related_type="invoice",
        related_id=str(invoice.id),
    )


def await_client_payment(
    billing: ServiceBilling, cycle: BillingCycle, now: datetime
) -> None:
    """Park a retail cycle whose invoice the client has not paid yet."""
    cycle.state = BillingCycleState.PENDING_ACTION
    cycle.claim_token = None
    cycle.claim_expires_at = None
    cycle.next_retry_at = now + RETAIL_RECHECK_INTERVAL
    billing.status = ServiceBillingStatus.PAST_DUE
    billing.next_retry_at = cycle.next_retry_at
    billing.last_failure_code = "awaiting_client_payment"
    billing.last_failure_message = "Renewal invoice is waiting for the client to pay"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def complete_retail_cycle(
    db: Session, cycle: BillingCycle, now: Optional[datetime] = None
) -> None:
    """Mark a paid retail cycle complete and schedule the next period."""
    current = now or datetime.now(timezone.utc)
    billing = cycle.service_billing
    if cycle.state == BillingCycleState.PAID:
        return
    cycle.state = BillingCycleState.PAID
    cycle.paid_at = (cycle.invoice.paid_at if cycle.invoice is not None else None) or current
    cycle.completed_at = current
    cycle.failure_code = None
    cycle.failure_message = None
    cycle.next_retry_at = None
    cycle.claim_token = None
    cycle.claim_expires_at = None
    if billing.status == ServiceBillingStatus.CANCELLED:
        db.flush()
        return
    billing.status = ServiceBillingStatus.ACTIVE
    billing.failed_attempts = 0
    billing.last_failure_code = None
    billing.last_failure_message = None
    billing.grace_until = None
    billing.next_retry_at = None
    billing.last_charged_at = cycle.paid_at
    billing.billing_anchor_day = billing.billing_anchor_day or _utc(cycle.due_at).day
    billing.next_charge_at = advance_months(
        _utc(cycle.due_at),
        billing_interval_months(billing),
        billing.billing_anchor_day,
    )
    db.flush()


def complete_retail_cycle_for_invoice(db: Session, invoice: Invoice) -> None:
    """Settlement subscriber step: complete the retail cycle a paid invoice belongs to."""
    if invoice.purpose != InvoicePurpose.CYCLE_CHARGE:
        return
    cycle = BillingLocks.billing_cycle(
        db, select(BillingCycle).where(BillingCycle.invoice_id == invoice.id)
    )
    if cycle is None or cycle.service_billing is None:
        return
    if not is_retail(cycle.service_billing):
        return
    if cycle.state == BillingCycleState.CANCELLED:
        return
    complete_retail_cycle(db, cycle)
