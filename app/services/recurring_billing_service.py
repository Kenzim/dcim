"""Crash-safe recurring billing, dunning, grace, and recovery state machine."""

from __future__ import annotations

import calendar
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
from app.services.invoice_service import InvoiceService
from app.services.notification_service import NotificationEvent, NotificationService
from app.services.payments.orchestrator import PaymentOrchestrator
from app.services.service_lifecycle import ServiceLifecycle, ServiceLifecycleError


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


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


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
        anchor = int(anchor_day or value.day)
        year = value.year + (1 if value.month == 12 else 0)
        month = 1 if value.month == 12 else value.month + 1
        day = min(anchor, calendar.monthrange(year, month)[1])
        return value.replace(year=year, month=month, day=day)

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

        due_statement = (
            select(ServiceBilling)
            .where(
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
                )
            )
            .order_by(
                ServiceBilling.next_retry_at,
                ServiceBilling.next_charge_at,
                ServiceBilling.id,
            )
            .limit(remaining)
        )
        billings = list(
            db.execute(self._locked(due_statement, db)).scalars()
        )
        for billing in billings:
            outcome = await self._process_billing(
                db, billing=billing, now=now, owner=owner
            )
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
        statement = (
            select(BillingCycle)
            .join(Invoice, Invoice.id == BillingCycle.invoice_id)
            .where(
                Invoice.status == InvoiceStatus.PAID,
                BillingCycle.state.not_in(
                    [
                        BillingCycleState.PAID,
                        BillingCycleState.CANCELLED,
                    ]
                ),
            )
            .order_by(BillingCycle.id)
            .limit(limit)
        )
        cycles = list(db.execute(self._locked(statement, db)).scalars())
        for cycle in cycles:
            await self._complete_paid_cycle(db, cycle, now)
            db.commit()
            result["processed"] += 1
            result["paid"] += 1
        return len(cycles)

    async def _process_billing(
        self,
        db: Session,
        *,
        billing: ServiceBilling,
        now: datetime,
        owner: str,
    ) -> str:
        service = billing.service
        if service is None or service.status == ServiceStatus.TERMINATED:
            self._cancel_billing(db, billing, now)
            db.commit()
            return "cancelled"

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
        existing = db.execute(
            select(BillingCycle).where(
                BillingCycle.service_billing_id == billing.id,
                BillingCycle.due_at == due_at,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        amount = billing.monthly_price_cents
        valid_amount = type(amount) is int and amount > 0
        currency = (billing.currency or "").strip().upper()
        valid_currency = len(currency) == 3 and currency.isalpha()
        cycle = BillingCycle(
            service_billing_id=billing.id,
            due_at=due_at,
            amount_cents=max(0, int(amount or 0)),
            currency=currency if valid_currency else "USD",
            state=(
                BillingCycleState.DUE
                if valid_amount and valid_currency
                else BillingCycleState.MANUAL_REVIEW
            ),
        )
        try:
            with db.begin_nested():
                db.add(cycle)
                db.flush()
                if valid_amount and valid_currency:
                    invoice = InvoiceService.create_cycle(
                        db,
                        reseller_id=billing.reseller_id,
                        service_id=billing.service_id,
                        amount_cents=amount,
                        currency=currency,
                        description=(
                            f"Recurring service charge for {billing.service.name}"
                        ),
                        due_at=due_at,
                    )
                    cycle.invoice_id = invoice.id
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
            return db.execute(
                select(BillingCycle).where(
                    BillingCycle.service_billing_id == billing.id,
                    BillingCycle.due_at == due_at,
                )
            ).scalar_one()
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
            funding = self.gateway.fund_invoice(
                db, billing.reseller, cycle.invoice
            )
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
        if cycle.invoice.status != InvoiceStatus.PENDING_ACTION:
            cycle.invoice.status = InvoiceStatus.OVERDUE
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
        billing.next_charge_at = self.next_calendar_month(
            _utc(cycle.due_at), billing.billing_anchor_day
        )
        if was_nonpayment_suspended:
            await self.lifecycle.unsuspend(
                db, billing.service, reason="payment_recovered"
            )

        if not self._has_overdue_cycles(db, reseller.id, excluding=cycle.id):
            await self._restore_cascade(db, reseller)
            if reseller.cached_balance_cents >= 0 and (
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
            if cycle.invoice is not None and cycle.invoice.status != InvoiceStatus.PAID:
                cycle.invoice.status = InvoiceStatus.VOID

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
