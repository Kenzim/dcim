"""Crash-safe recurring billing, dunning, grace, and recovery state machine."""

from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    RecurringBillingLease,
    Reseller,
    ResellerNonpaymentPolicy,
    ServiceBilling,
    ServiceBillingStatus,
)
from app.models.service import ServiceStatus
from app.services.billing.intervals import advance_months
from app.services.billing.operator_currency import operator_currency
from app.services.billing_locks import BillingLocks
from app.services.billing.renewals import (
    AccountFunder,
    ResellerFunder,
    RetailInvoiceFunder,
    await_client_payment,
    billing_interval_months,
    complete_retail_cycle,
    is_retail,
)
from app.services.invoice_service import InvoiceService
from app.services.notification_service import NotificationEvent, NotificationService
from app.services.credit_ledger_service import CreditLedgerService
from app.services.payments.orchestrator import PaymentOrchestrator
from app.services.service_lifecycle import ServiceLifecycle, ServiceLifecycleError
from app.services.tax.errors import RetryableTaxPublishError

logger = logging.getLogger(__name__)


_SAFE_CODE_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_OVERDUE_STATES = {
    BillingCycleState.PAST_DUE,
    BillingCycleState.GRACE,
    BillingCycleState.PENDING_ACTION,
    BillingCycleState.SUSPENDED,
}
_TERMINAL_STATES = {
    BillingCycleState.PAID,
    BillingCycleState.CANCELLED,
    BillingCycleState.MANUAL_REVIEW,
}
# Invoice statuses a failed renewal never chases: the cycle goes to manual review.
_UNCHASED_INVOICE_STATUSES = frozenset(
    {InvoiceStatus.VOID, InvoiceStatus.REFUNDED, InvoiceStatus.FAILED, InvoiceStatus.DRAFT}
)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def cycle_snapshot(billing: ServiceBilling) -> tuple[int, str, bool]:
    """The amount and currency a new cycle of ``billing`` charges, and whether they are valid."""
    amount = billing.monthly_price_cents
    valid_amount = type(amount) is int and amount > 0
    currency = (billing.currency or "").strip().upper()
    valid_currency = len(currency) == 3 and currency.isalpha()
    return (
        max(0, int(amount or 0)),
        currency if valid_currency else operator_currency(),
        valid_amount and valid_currency,
    )


def create_cycle_invoice(
    db: Session,
    billing: ServiceBilling,
    cycle: BillingCycle,
    *,
    period_start: Optional[datetime] = None,
    period_end: Optional[datetime] = None,
) -> Invoice:
    """Create the cycle invoice of a new, flushed ``cycle`` and link it.

    The one path both the at-due worker and ``generate_invoices_ahead`` use,
    so a cycle generated ahead is priced exactly as one created at due.
    """
    invoice = InvoiceService.create_cycle(
        db,
        reseller_id=billing.reseller_id,
        service_id=billing.service_id,
        amount_cents=billing.monthly_price_cents,
        currency=cycle.currency,
        description=f"Recurring service charge for {billing.service.name}",
        due_at=_utc(cycle.due_at),
        billing_account_id=billing.billing_account_id if is_retail(billing) else None,
        period_start=period_start,
        period_end=period_end,
    )
    cycle.invoice_id = invoice.id
    return invoice


def lock_cycle_for_due(
    db: Session, billing_id: int, due_at: datetime
) -> Optional[BillingCycle]:
    """Lock the cycle of a billing for one due date; the caller holds the service billing.

    A locking read, so a cycle another transaction (``generate_invoices_ahead``)
    committed after this transaction's snapshot is seen and reused.
    """
    return BillingLocks.billing_cycle(
        db,
        select(BillingCycle).where(
            BillingCycle.service_billing_id == billing_id,
            BillingCycle.due_at == _utc(due_at),
        ),
    )


class RecurringBillingService:
    LEASE_NAME = "recurring-billing"

    def __init__(
        self,
        *,
        config: Settings = settings,
        gateway: Optional[PaymentOrchestrator] = None,
        lifecycle: Optional[ServiceLifecycle] = None,
        notifier=NotificationService,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.gateway = gateway or PaymentOrchestrator()
        self.lifecycle = lifecycle or ServiceLifecycle()
        self.notifier = notifier
        self.clock = clock

    @staticmethod
    def next_calendar_month(value: datetime, anchor_day: Optional[int] = None) -> datetime:
        """Advance one calendar month while retaining a month-end anchor."""
        return advance_months(value, 1, anchor_day)

    def _retail_enabled(self) -> bool:
        return bool(getattr(self.config, "billing_retail_renewals_enabled", False))

    def _funder_for(self, billing: ServiceBilling) -> AccountFunder:
        if is_retail(billing):
            return RetailInvoiceFunder()
        return ResellerFunder(self.gateway)

    @staticmethod
    def _locked(statement, db: Session):
        if db.get_bind().dialect.name in {"mysql", "postgresql"}:
            return statement.with_for_update(skip_locked=True)
        return statement.with_for_update()

    def acquire_lease(
        self,
        db: Session,
        *,
        owner: str,
        now: Optional[datetime] = None,
    ) -> bool:
        current = now or self.clock()
        if db.get(RecurringBillingLease, self.LEASE_NAME) is None:
            try:
                with db.begin_nested():
                    db.add(RecurringBillingLease(name=self.LEASE_NAME))
                    db.flush()
            except IntegrityError:
                pass
        result = db.execute(
            update(RecurringBillingLease)
            .where(
                RecurringBillingLease.name == self.LEASE_NAME,
                or_(
                    RecurringBillingLease.owner.is_(None),
                    RecurringBillingLease.expires_at.is_(None),
                    RecurringBillingLease.expires_at <= current,
                    RecurringBillingLease.owner == owner,
                ),
            )
            .values(
                owner=owner,
                expires_at=current
                + timedelta(
                    seconds=self.config.recurring_billing_worker_lease_seconds
                ),
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def release_lease(self, db: Session, *, owner: str) -> None:
        db.execute(
            update(RecurringBillingLease)
            .where(
                RecurringBillingLease.name == self.LEASE_NAME,
                RecurringBillingLease.owner == owner,
            )
            .values(owner=None, expires_at=None)
        )

    async def process_batch(
        self,
        db: Session,
        *,
        batch_size: Optional[int] = None,
        owner: str = "manual",
    ) -> dict[str, int]:
        now = self.clock()
        limit = batch_size or self.config.recurring_billing_worker_batch_size
        result = {
            "processed": 0,
            "paid": 0,
            "failed": 0,
            "pending_action": 0,
            "suspended": 0,
            "cancelled": 0,
            "manual_review": 0,
        }

        recovered = await self._recover_paid_cycles(
            db, now=now, limit=limit, result=result
        )
        remaining = max(0, limit - recovered)
        if remaining == 0:
            return result

        due_conditions = [
            or_(
                and_(
                    ServiceBilling.status == ServiceBillingStatus.ACTIVE,
                    ServiceBilling.next_charge_at.is_not(None),
                    ServiceBilling.next_charge_at <= now,
                ),
                and_(
                    ServiceBilling.status.in_(
                        [
                            ServiceBillingStatus.PAST_DUE,
                            ServiceBillingStatus.GRACE,
                        ]
                    ),
                    or_(
                        ServiceBilling.next_retry_at.is_(None),
                        ServiceBilling.next_retry_at <= now,
                        ServiceBilling.grace_until <= now,
                    ),
                ),
                and_(
                    ServiceBilling.cancel_at.is_not(None),
                    ServiceBilling.cancel_at <= now,
                    ServiceBilling.status != ServiceBillingStatus.CANCELLED,
                ),
            )
        ]
        if not self._retail_enabled():
            due_conditions.append(
                or_(
                    ServiceBilling.reseller_id.is_not(None),
                    and_(
                        ServiceBilling.cancel_at.is_not(None),
                        ServiceBilling.cancel_at <= now,
                    ),
                )
            )
        due_statement = (
            select(ServiceBilling)
            .where(*due_conditions)
            .order_by(
                ServiceBilling.next_retry_at,
                ServiceBilling.next_charge_at,
                ServiceBilling.id,
            )
            .limit(remaining)
        )
        billing_ids = [
            candidate.id
            for candidate in db.execute(self._locked(due_statement, db)).scalars()
        ]
        for billing_id in billing_ids:
            # Each billing is processed in a transaction that starts with its
            # lock, so the transaction's snapshot is taken after the lock and
            # sees a cycle and invoice generated ahead (design 10.1) and
            # committed by a job that held the lock before it. Ending the
            # transaction also releases the batch's locks, so each row is
            # locked again and re-checked before it is processed.
            db.commit()
            billing = BillingLocks.service_billing(
                db, billing_id, where=due_conditions, skip_locked=True
            )
            if billing is None:
                continue
            try:
                outcome = await self._process_billing(
                    db, billing=billing, now=now, owner=owner
                )
            except RetryableTaxPublishError as exc:
                # Nothing of this billing is kept; it is still due on the next pass.
                db.rollback()
                logger.warning(
                    "Recurring billing %s: tax could not be frozen on its invoice (%s); retried next pass",
                    billing_id,
                    exc,
                )
                continue
            result["processed"] += 1
            if outcome in result:
                result[outcome] += 1
        return result

    async def _recover_paid_cycles(
        self,
        db: Session,
        *,
        now: datetime,
        limit: int,
        result: dict[str, int],
    ) -> int:
        conditions = (
            Invoice.status == InvoiceStatus.PAID,
            BillingCycle.state.not_in(
                [
                    BillingCycleState.PAID,
                    BillingCycleState.CANCELLED,
                ]
            ),
        )
        candidates = db.execute(
            select(BillingCycle.id, BillingCycle.service_billing_id)
            .join(Invoice, Invoice.id == BillingCycle.invoice_id)
            .where(*conditions)
            .order_by(BillingCycle.id)
            .limit(limit)
        ).all()
        recovered = 0
        for cycle_id, billing_id in candidates:
            # Billing lock order: the service billing, then its cycle.
            if billing_id is not None and BillingLocks.service_billing(
                db, int(billing_id), skip_locked=True
            ) is None:
                continue
            cycle = BillingLocks.billing_cycle(
                db,
                select(BillingCycle)
                .join(Invoice, Invoice.id == BillingCycle.invoice_id)
                .where(BillingCycle.id == cycle_id, *conditions),
                skip_locked=True,
            )
            if cycle is None:
                db.rollback()
                continue
            if is_retail(cycle.service_billing):
                complete_retail_cycle(db, cycle, now)
            else:
                await self._complete_paid_cycle(db, cycle, now)
            db.commit()
            recovered += 1
            result["processed"] += 1
            result["paid"] += 1
        return recovered

    async def _process_billing(
        self,
        db: Session,
        *,
        billing: ServiceBilling,
        now: datetime,
        owner: str,
    ) -> str:
        service = billing.service
        if billing.cancel_at is not None and _utc(billing.cancel_at) <= now:
            ended = await self._end_at_cancel_date(db, billing, now)
            db.commit()
            return "cancelled" if ended else "manual_review"
        if service is None or service.status == ServiceStatus.TERMINATED:
            self._cancel_billing(db, billing, now)
            db.commit()
            return "cancelled"
        if is_retail(billing):
            return self._process_retail_billing(db, billing=billing, now=now)

        cycle = self._current_cycle(db, billing)
        if cycle is None:
            cycle = self._ensure_cycle(db, billing, now)
            db.commit()  # Invoice identity must survive any later gateway crash.
        initial = await self._initial_cycle_outcome(
            db, billing=billing, cycle=cycle, now=now, owner=owner
        )
        if initial is not None:
            return initial
        suspension = await self._suspension_outcome(db, billing, cycle, now)
        if suspension is not None:
            return suspension
        pending = self._handle_pending_action(db, billing, cycle)
        if pending is not None:
            return pending
        if self._retry_is_not_due(cycle, now):
            db.rollback()
            return "processed"
        return await self._attempt_payment(
            db, billing=billing, cycle=cycle, now=now, owner=owner
        )

    async def _end_at_cancel_date(
        self, db: Session, billing: ServiceBilling, now: datetime
    ) -> bool:
        """Terminate the service and cancel the billing; False if termination failed."""
        from app.services.commerce_lifecycle_service import (
            ServiceTerminationError,
            terminate_service,
        )

        service = billing.service
        if service is not None:
            try:
                await terminate_service(
                    db, service, lifecycle=self.lifecycle, now=now, reason="end_of_period_cancel"
                )
            except ServiceTerminationError as exc:
                # Billing stays due (cancel_at passed, not cancelled), so the
                # next pass retries; nothing is charged while cancel_at is set.
                billing.last_failure_code = "end_of_period_terminate_failed"
                billing.last_failure_message = str(exc)[:500]
                return False
        self._cancel_billing(db, billing, now)
        billing.last_failure_code = "end_of_cycle_cancel"
        return True

    def _process_retail_billing(
        self,
        db: Session,
        *,
        billing: ServiceBilling,
        now: datetime,
    ) -> str:
        cycle = self._current_cycle(db, billing)
        if cycle is None:
            cycle = self._ensure_cycle(db, billing, now)
            db.commit()
        if cycle.state == BillingCycleState.MANUAL_REVIEW:
            return "manual_review"
        if cycle.invoice is None:
            self._mark_manual(
                billing,
                cycle,
                code="missing_invoice",
                message="Billing cycle has no invoice",
            )
            db.commit()
            return "manual_review"
        if cycle.invoice.status == InvoiceStatus.PAID:
            complete_retail_cycle(db, cycle, now)
            db.commit()
            return "paid"
        if cycle.invoice.status == InvoiceStatus.VOID:
            self._mark_manual(
                billing,
                cycle,
                code="invoice_void",
                message="Renewal invoice was voided; decide whether to reissue or cancel",
            )
            db.commit()
            return "manual_review"
        funding = self._funder_for(billing).fund(db, billing, cycle)
        if funding.funded:
            InvoiceService.mark_paid_if_fully_allocated(db, cycle.invoice_id)
            complete_retail_cycle(db, cycle, now)
            db.commit()
            return "paid"
        await_client_payment(billing, cycle, now)
        db.commit()
        return "pending_action"

    async def _initial_cycle_outcome(
        self,
        db: Session,
        *,
        billing: ServiceBilling,
        cycle: BillingCycle,
        now: datetime,
        owner: str,
    ) -> Optional[str]:
        if cycle.state == BillingCycleState.MANUAL_REVIEW:
            return "manual_review"
        if cycle.state == BillingCycleState.ERROR and cycle.invoice is None:
            return "manual_review"
        if cycle.invoice is None:
            self._mark_manual(
                billing,
                cycle,
                code="missing_invoice",
                message="Billing cycle has no invoice",
            )
            db.commit()
            return "manual_review"
        if cycle.invoice.status == InvoiceStatus.PAID:
            await self._complete_paid_cycle(db, cycle, now)
            db.commit()
            return "paid"
        if self._claimed_by_another_worker(cycle, now, owner):
            db.rollback()
            return "processed"
        return None

    @staticmethod
    def _claimed_by_another_worker(
        cycle: BillingCycle, now: datetime, owner: str
    ) -> bool:
        return bool(
            cycle.state == BillingCycleState.PROCESSING
            and cycle.claim_expires_at is not None
            and _utc(cycle.claim_expires_at) > now
            and cycle.claim_token != owner
        )

    def _handle_pending_action(
        self,
        db: Session,
        billing: ServiceBilling,
        cycle: BillingCycle,
    ) -> Optional[str]:
        if cycle.state != BillingCycleState.PENDING_ACTION:
            return None
        if cycle.invoice.status != InvoiceStatus.PENDING_ACTION:
            self._schedule_after_pending_failure(billing, cycle)
            db.commit()
            return None
        self._notify(
            db,
            billing.reseller,
            NotificationEvent.PAYMENT_ACTION_REQUIRED,
            f"billing-cycle:{cycle.id}:action-required",
            self._notification_data(billing, cycle),
        )
        db.commit()
        return "pending_action"

    async def _suspension_outcome(
        self,
        db: Session,
        billing: ServiceBilling,
        cycle: BillingCycle,
        now: datetime,
    ) -> Optional[str]:
        if not self._should_suspend(cycle, now):
            return None
        suspended = await self._suspend_for_nonpayment(db, billing, cycle, now)
        db.commit()
        return "suspended" if suspended else "failed"

    def _retry_is_not_due(self, cycle: BillingCycle, now: datetime) -> bool:
        exhausted_in_grace = bool(
            cycle.attempts
            > len(self.config.recurring_billing_retry_day_offsets)
            and cycle.grace_until is not None
            and _utc(cycle.grace_until) > now
        )
        future_retry = bool(
            cycle.next_retry_at is not None
            and _utc(cycle.next_retry_at) > now
            and cycle.state != BillingCycleState.DUE
        )
        return exhausted_in_grace or future_retry

    def _current_cycle(
        self, db: Session, billing: ServiceBilling
    ) -> Optional[BillingCycle]:
        return db.execute(
            select(BillingCycle)
            .where(
                BillingCycle.service_billing_id == billing.id,
                BillingCycle.state.not_in(list(_TERMINAL_STATES)),
            )
            .order_by(BillingCycle.due_at.desc(), BillingCycle.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _ensure_cycle(
        self,
        db: Session,
        billing: ServiceBilling,
        now: datetime,
    ) -> BillingCycle:
        due_at = _utc(billing.next_charge_at or now)
        existing = lock_cycle_for_due(db, billing.id, due_at)
        if existing is not None:
            return existing

        amount, currency, valid = cycle_snapshot(billing)
        cycle = BillingCycle(
            service_billing_id=billing.id,
            due_at=due_at,
            amount_cents=amount,
            currency=currency,
            state=BillingCycleState.DUE if valid else BillingCycleState.MANUAL_REVIEW,
        )
        try:
            with db.begin_nested():
                db.add(cycle)
                db.flush()
                if valid:
                    create_cycle_invoice(db, billing, cycle)
                else:
                    cycle.failure_code = "invalid_billing_snapshot"
                    cycle.failure_message = (
                        "Monthly price or currency requires manual review"
                    )
                    billing.status = ServiceBillingStatus.MANUAL_REVIEW
                billing.billing_anchor_day = (
                    billing.billing_anchor_day or due_at.day
                )
                db.flush()
        except IntegrityError:
            existing = lock_cycle_for_due(db, billing.id, due_at)
            if existing is None:
                raise
            return existing
        return cycle

    async def _attempt_payment(
        self,
        db: Session,
        *,
        billing: ServiceBilling,
        cycle: BillingCycle,
        now: datetime,
        owner: str,
    ) -> str:
        cycle.state = BillingCycleState.PROCESSING
        cycle.claim_token = owner
        cycle.claim_expires_at = now + timedelta(
            seconds=self.config.recurring_billing_worker_lease_seconds
        )
        db.commit()
        try:
            funding = self._funder_for(billing).fund(db, billing, cycle)
        except Exception as exc:
            db.rollback()
            billing = db.get(ServiceBilling, billing.id)
            cycle = db.get(BillingCycle, cycle.id)
            self._record_failure(
                db,
                billing,
                cycle,
                now,
                code=type(exc).__name__,
                message="Payment processing failed",
            )
            db.commit()
            return "failed"

        cycle.attempts += 1
        cycle.last_attempt_at = now
        cycle.claim_token = None
        cycle.claim_expires_at = None
        if funding.funded:
            InvoiceService.mark_paid_if_fully_allocated(db, cycle.invoice_id)
            await self._complete_paid_cycle(db, cycle, now)
            db.commit()
            return "paid"
        if funding.pending_action:
            self._record_pending_action(db, billing, cycle, now)
            db.commit()
            return "pending_action"
        self._record_failure(db, billing, cycle, now, increment=False)
        db.commit()
        return "failed"

    def _record_pending_action(
        self,
        db: Session,
        billing: ServiceBilling,
        cycle: BillingCycle,
        now: datetime,
    ) -> None:
        cycle.state = BillingCycleState.PENDING_ACTION
        cycle.grace_until = _utc(cycle.due_at) + timedelta(
            days=self.config.recurring_billing_grace_days
        )
        cycle.next_retry_at = None
        billing.status = (
            ServiceBillingStatus.PAST_DUE
            if cycle.attempts <= 1
            else ServiceBillingStatus.GRACE
        )
        billing.failed_attempts = cycle.attempts
        billing.grace_until = cycle.grace_until
        billing.next_retry_at = None
        billing.last_failure_code = "payment_action_required"
        billing.last_failure_message = "Payment action is required"
        self._apply_hold(billing.reseller, now)
        self._notify(
            db,
            billing.reseller,
            NotificationEvent.PAYMENT_ACTION_REQUIRED,
            f"billing-cycle:{cycle.id}:action-required",
            self._notification_data(billing, cycle),
        )

    def _record_failure(
        self,
        db: Session,
        billing: ServiceBilling,
        cycle: BillingCycle,
        now: datetime,
        *,
        code: Optional[str] = None,
        message: Optional[str] = None,
        increment: bool = True,
    ) -> None:
        if increment:
            cycle.attempts += 1
            cycle.last_attempt_at = now
        # The invoice lock (after the service billing, LOCK_ORDER) re-reads the
        # status, so a void or payment committed meanwhile is seen.
        invoice = (
            BillingLocks.for_invoice(db, cycle.invoice_id)
            if cycle.invoice_id is not None
            else None
        )
        if invoice is None or invoice.status in _UNCHASED_INVOICE_STATUSES:
            cycle.claim_token = None
            cycle.claim_expires_at = None
            status = invoice.status.value if invoice is not None else "missing"
            self._mark_manual(
                billing,
                cycle,
                code=f"invoice_{status}",
                message=(
                    f"Renewal invoice is {status}; decide whether to reissue or cancel"
                ),
            )
            return
        if invoice.status == InvoiceStatus.PAID:
            # Paid meanwhile: the paid-cycle recovery completes the cycle.
            cycle.claim_token = None
            cycle.claim_expires_at = None
            return
        safe_code, safe_message = self._last_safe_failure(
            db, cycle.invoice_id, code=code, message=message
        )
        cycle.failure_code = safe_code
        cycle.failure_message = safe_message
        cycle.claim_token = None
        cycle.claim_expires_at = None
        cycle.grace_until = _utc(cycle.due_at) + timedelta(
            days=self.config.recurring_billing_grace_days
        )
        retry_days = self.config.recurring_billing_retry_day_offsets
        cycle.next_retry_at = (
            _utc(cycle.due_at) + timedelta(days=retry_days[cycle.attempts - 1])
            if 0 < cycle.attempts <= len(retry_days)
            else None
        )
        cycle.state = (
            BillingCycleState.PAST_DUE
            if cycle.attempts == 1
            else BillingCycleState.GRACE
        )
        if invoice.status in (InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID):
            InvoiceService.transition(
                db, invoice, InvoiceStatus.OVERDUE, cause="renewal_past_due"
            )
        billing.status = (
            ServiceBillingStatus.PAST_DUE
            if cycle.attempts == 1
            else ServiceBillingStatus.GRACE
        )
        billing.failed_attempts = cycle.attempts
        billing.last_failure_code = safe_code
        billing.last_failure_message = safe_message
        billing.grace_until = cycle.grace_until
        billing.next_retry_at = cycle.next_retry_at
        self._apply_hold(billing.reseller, now)
        data = self._notification_data(billing, cycle)
        self._notify(
            db,
            billing.reseller,
            NotificationEvent.CYCLE_CHARGE_FAILED,
            f"billing-cycle:{cycle.id}:failed:{cycle.attempts}",
            data,
        )
        if cycle.attempts == 2:
            self._notify(
                db,
                billing.reseller,
                NotificationEvent.GRACE_WARNING,
                f"billing-cycle:{cycle.id}:grace-warning",
                data,
            )

    def _schedule_after_pending_failure(
        self, billing: ServiceBilling, cycle: BillingCycle
    ) -> None:
        retry_days = self.config.recurring_billing_retry_day_offsets
        cycle.next_retry_at = (
            _utc(cycle.due_at) + timedelta(days=retry_days[cycle.attempts - 1])
            if 0 < cycle.attempts <= len(retry_days)
            else None
        )
        cycle.state = (
            BillingCycleState.PAST_DUE
            if cycle.attempts <= 1
            else BillingCycleState.GRACE
        )
        billing.status = (
            ServiceBillingStatus.PAST_DUE
            if cycle.attempts <= 1
            else ServiceBillingStatus.GRACE
        )
        billing.next_retry_at = cycle.next_retry_at

    @staticmethod
    def _last_safe_failure(
        db: Session,
        invoice_id: int,
        *,
        code: Optional[str],
        message: Optional[str],
    ) -> tuple[str, str]:
        payment = db.execute(
            select(Payment)
            .where(
                Payment.invoice_id == invoice_id,
                Payment.status == PaymentStatus.FAILED,
            )
            .order_by(Payment.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        raw_code = code or (payment.failure_code if payment else None) or "payment_failed"
        safe_code = _SAFE_CODE_RE.sub("_", str(raw_code))[:128] or "payment_failed"
        safe_message = message or "The recurring payment could not be completed"
        return safe_code, safe_message[:500]

    def _apply_hold(self, reseller: Reseller, now: datetime) -> None:
        if reseller.nonpayment_policy != ResellerNonpaymentPolicy.BLOCK_NEW:
            return
        if not reseller.billing_hold:
            reseller.billing_hold_at = now
        reseller.billing_hold = True
        reseller.billing_hold_reason = "nonpayment"
        reseller.billing_hold_cleared_at = None

    def _should_suspend(self, cycle: BillingCycle, now: datetime) -> bool:
        exhausted = (
            cycle.state == BillingCycleState.PENDING_ACTION
            or cycle.attempts
            > len(self.config.recurring_billing_retry_day_offsets)
        )
        return bool(
            exhausted
            and cycle.grace_until is not None
            and _utc(cycle.grace_until) <= now
        )

    async def _suspend_for_nonpayment(
        self,
        db: Session,
        billing: ServiceBilling,
        cycle: BillingCycle,
        now: datetime,
    ) -> bool:
        rows = self._suspension_rows(db, billing)
        target_suspended = False
        lifecycle_failed = False
        for row in rows:
            marked, failed = await self._suspend_billing_row(db, row, cycle)
            lifecycle_failed = lifecycle_failed or failed
            target_suspended = target_suspended or (
                row.id == billing.id and marked
            )
        if lifecycle_failed and not target_suspended:
            self._record_suspension_failure(billing, cycle, now)
            return False
        if not target_suspended:
            billing.status = ServiceBillingStatus.MANUAL_REVIEW
            billing.last_failure_code = "service_already_suspended"
            billing.last_failure_message = (
                "Service was already suspended outside recurring billing"
            )
            cycle.failure_code = billing.last_failure_code
            cycle.failure_message = billing.last_failure_message
        cycle.state = BillingCycleState.SUSPENDED
        cycle.completed_at = now
        cycle.next_retry_at = None
        self._set_nonpayment_hold(billing.reseller, now)
        return target_suspended

    @staticmethod
    def _suspension_rows(
        db: Session, billing: ServiceBilling
    ) -> list[ServiceBilling]:
        if (
            billing.reseller.nonpayment_policy
            != ResellerNonpaymentPolicy.SUSPEND_ALL
        ):
            return [billing]
        return list(
            db.execute(
                select(ServiceBilling)
                .where(
                    ServiceBilling.reseller_id == billing.reseller_id,
                    ServiceBilling.status != ServiceBillingStatus.CANCELLED,
                )
                .order_by(ServiceBilling.id)
                .with_for_update()
            ).scalars()
        )

    async def _suspend_billing_row(
        self,
        db: Session,
        row: ServiceBilling,
        cycle: BillingCycle,
    ) -> tuple[bool, bool]:
        service = row.service
        if service is None or service.status == ServiceStatus.TERMINATED:
            return False, False
        try:
            changed = await self.lifecycle.suspend(
                db, service, reason="nonpayment"
            )
        except ServiceLifecycleError:
            return False, True
        marked = bool(
            changed
            or row.status == ServiceBillingStatus.SUSPENDED_NONPAYMENT
        )
        if not marked:
            return False, False
        row.status = ServiceBillingStatus.SUSPENDED_NONPAYMENT
        row.next_retry_at = None
        self._notify(
            db,
            row.reseller,
            NotificationEvent.SERVICE_SUSPENDED,
            f"billing-cycle:{cycle.id}:suspended-service:{row.service_id}",
            self._notification_data(row, cycle),
        )
        return True, False

    @staticmethod
    def _record_suspension_failure(
        billing: ServiceBilling,
        cycle: BillingCycle,
        now: datetime,
    ) -> None:
        cycle.state = BillingCycleState.GRACE
        cycle.next_retry_at = now + timedelta(hours=1)
        cycle.failure_code = "lifecycle_suspend_failed"
        cycle.failure_message = "Service suspension could not be completed"
        billing.next_retry_at = cycle.next_retry_at

    @staticmethod
    def _set_nonpayment_hold(reseller: Reseller, now: datetime) -> None:
        if not reseller.billing_hold:
            reseller.billing_hold_at = now
        reseller.billing_hold = True
        reseller.billing_hold_reason = "nonpayment"
        reseller.billing_hold_cleared_at = None

    async def _complete_paid_cycle(
        self,
        db: Session,
        cycle: BillingCycle,
        now: datetime,
    ) -> None:
        billing = cycle.service_billing
        reseller = billing.reseller
        recovered_payment = (
            cycle.state in _OVERDUE_STATES
            or billing.status
            in {
                ServiceBillingStatus.PAST_DUE,
                ServiceBillingStatus.GRACE,
                ServiceBillingStatus.SUSPENDED_NONPAYMENT,
            }
        )
        was_nonpayment_suspended = (
            billing.status == ServiceBillingStatus.SUSPENDED_NONPAYMENT
        )
        cycle.state = BillingCycleState.PAID
        cycle.paid_at = cycle.invoice.paid_at or now
        cycle.completed_at = now
        cycle.failure_code = None
        cycle.failure_message = None
        cycle.next_retry_at = None
        cycle.claim_token = None
        cycle.claim_expires_at = None
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
        if was_nonpayment_suspended:
            await self.lifecycle.unsuspend(
                db, billing.service, reason="payment_recovered"
            )

        if not self._has_overdue_cycles(db, reseller.id, excluding=cycle.id):
            await self._restore_cascade(db, reseller)
            if CreditLedgerService.balance_cents(db, reseller) >= 0 and (
                not reseller.billing_hold
                or reseller.billing_hold_reason == "nonpayment"
            ):
                if reseller.billing_hold:
                    reseller.billing_hold_cleared_at = now
                reseller.billing_hold = False
                reseller.billing_hold_reason = None
        if recovered_payment:
            self._notify(
                db,
                reseller,
                NotificationEvent.PAYMENT_RECOVERED,
                f"billing-cycle:{cycle.id}:payment-recovered",
                self._notification_data(billing, cycle),
            )

    def _has_overdue_cycles(
        self,
        db: Session,
        reseller_id: int,
        *,
        excluding: Optional[int] = None,
    ) -> bool:
        statement = (
            select(BillingCycle.id)
            .join(ServiceBilling)
            .where(
                ServiceBilling.reseller_id == reseller_id,
                BillingCycle.state.in_(list(_OVERDUE_STATES)),
            )
            .limit(1)
        )
        if excluding is not None:
            statement = statement.where(BillingCycle.id != excluding)
        return db.scalar(statement) is not None

    async def _restore_cascade(self, db: Session, reseller: Reseller) -> None:
        rows = list(
            db.execute(
                select(ServiceBilling).where(
                    ServiceBilling.reseller_id == reseller.id,
                    ServiceBilling.status
                    == ServiceBillingStatus.SUSPENDED_NONPAYMENT,
                )
            ).scalars()
        )
        for row in rows:
            if row.service.status == ServiceStatus.SUSPENDED:
                await self.lifecycle.unsuspend(
                    db, row.service, reason="payment_recovered"
                )
            row.status = ServiceBillingStatus.ACTIVE

    @staticmethod
    def _cancel_billing(
        db: Session, billing: ServiceBilling, now: datetime
    ) -> None:
        billing.status = ServiceBillingStatus.CANCELLED
        billing.next_charge_at = None
        billing.next_retry_at = None
        cycle = db.execute(
            select(BillingCycle)
            .where(
                BillingCycle.service_billing_id == billing.id,
                BillingCycle.state.not_in(
                    [
                        BillingCycleState.PAID,
                        BillingCycleState.CANCELLED,
                    ]
                ),
            )
            .order_by(BillingCycle.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if cycle is not None:
            cycle.state = BillingCycleState.CANCELLED
            cycle.completed_at = now
            cycle.next_retry_at = None
            if cycle.invoice is not None:
                RecurringBillingService._void_cancelled_cycle_invoice(db, cycle.invoice)

    @staticmethod
    def _void_cancelled_cycle_invoice(db: Session, invoice: Invoice) -> None:
        """Void the invoice of a cancelled cycle where the invoice engine allows it.

        The invoice is locked (after the service billing, and in ``v2`` after
        the reseller and billing account, LOCK_ORDER) and decided from the
        re-read row. Legacy and shadow void every invoice that is not
        ``paid`` or ``void``, as they always have. In ``v2`` only
        ``InvoiceService.void()`` voids (design 3.4): an ``open`` or
        ``overdue`` invoice with nothing paid, pending, noted or being
        refunded. An unpaid invoice it refuses is credited for what is still
        owed when no money is held, else recorded for refund review
        (``close_kept_invoice_v2``); the cancellation goes ahead either way.
        """
        from app.services.billing.order_refunds import (
            close_kept_invoice_v2,
            void_cancelled_invoice_v2,
        )
        from app.services.billing_shadow import billing_engine_mode

        if billing_engine_mode(db, "invoice") != "v2":
            locked = BillingLocks.for_invoice(db, invoice.id) or invoice
            if locked.status not in (InvoiceStatus.PAID, InvoiceStatus.VOID):
                InvoiceService.transition(
                    db,
                    locked,
                    InvoiceStatus.VOID,
                    cause="service_billing_cancelled",
                    reason="Service billing cancelled",
                )
            return
        locked = (
            BillingLocks.for_invoice(db, invoice.id, reseller=True, billing_account=True)
            or invoice
        )
        if void_cancelled_invoice_v2(
            db, locked, reason="Service billing cancelled", actor_user_id=None
        ):
            return
        close_kept_invoice_v2(
            db, locked, reason="service_billing_cancelled", actor_user_id=None
        )

    @staticmethod
    def _mark_manual(
        billing: ServiceBilling,
        cycle: BillingCycle,
        *,
        code: str,
        message: str,
    ) -> None:
        cycle.state = BillingCycleState.MANUAL_REVIEW
        cycle.failure_code = code
        cycle.failure_message = message
        billing.status = ServiceBillingStatus.MANUAL_REVIEW
        billing.last_failure_code = code
        billing.last_failure_message = message
        billing.next_retry_at = None

    @staticmethod
    def _notification_data(
        billing: ServiceBilling, cycle: BillingCycle
    ) -> dict:
        return {
            "invoice_id": cycle.invoice_id,
            "invoice_number": (
                cycle.invoice.invoice_number if cycle.invoice is not None else None
            ),
            "service_id": billing.service_id,
            "service_name": billing.service.name if billing.service is not None else "",
            "amount_cents": cycle.amount_cents,
            "currency": cycle.currency,
            "grace_until": (
                cycle.grace_until.isoformat() if cycle.grace_until else None
            ),
        }

    def _notify(
        self,
        db: Session,
        reseller: Reseller,
        event: str,
        key: str,
        data: dict,
    ) -> None:
        try:
            self.notifier.enqueue(
                db,
                reseller=reseller,
                event=event,
                idempotency_key=key,
                data=data,
            )
        except ValueError:
            # Missing/invalid recipient must never stop billing progression.
            return
