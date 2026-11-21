"""Billing A0 regression tests: interval-aware renewals, retail cycle processing, end-of-period cancel."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.dao.commerce_dao import BillingAccountDAO
from app.models.commerce_email import EmailMessage
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Reseller,
    ServiceBilling,
    ServiceBillingStatus,
)
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User
from app.services.billing.intervals import advance_months, interval_months
from app.services.billing.manual_payments import record_manual_payment
from app.services.commerce_lifecycle_service import CommerceLifecycleService
from app.services.commerce_recurring_service import CommerceRecurringService
from app.services.invoice_service import InvoiceService
from app.services.payments.orchestrator import InvoiceFundingResult
from app.services.recurring_billing_service import RecurringBillingService


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class PayingGateway:
    """Reseller funding stand-in that always succeeds."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def fund_invoice(self, db, reseller, invoice):
        self.calls.append(invoice.id)
        payment = Payment(
            invoice_id=invoice.id,
            gateway="fake",
            amount_cents=invoice.amount_cents,
            currency=invoice.currency,
            status=PaymentStatus.SUCCEEDED,
            external_ref=f"fake:{invoice.id}:{len(self.calls)}",
            processed_at=datetime.now(timezone.utc),
        )
        db.add(payment)
        db.flush()
        InvoiceService.mark_paid_if_fully_allocated(db, invoice.id)
        return InvoiceFundingResult(
            funded=True, allocated_cents=invoice.amount_cents, remaining_cents=0,
            pending_action=False, payment_id=payment.id,
        )


class Lifecycle:
    def __init__(self) -> None:
        self.suspended: list[int] = []

    async def suspend(self, db, service, *, reason):
        self.suspended.append(service.id)
        service.status = ServiceStatus.SUSPENDED
        db.flush()
        return True

    async def unsuspend(self, db, service, *, reason):
        service.status = ServiceStatus.ACTIVE
        db.flush()
        return True


def _config(**overrides) -> Settings:
    values = {
        "database_url": "sqlite://",
        "recurring_billing_retry_days": "1,3,5",
        "recurring_billing_grace_days": 7,
        "recurring_billing_worker_batch_size": 100,
    }
    values.update(overrides)
    return Settings(**values)


def _worker(clock, *, gateway=None, lifecycle=None, retail=False) -> RecurringBillingService:
    return RecurringBillingService(
        config=_config(billing_retail_renewals_enabled=retail),
        gateway=gateway or PayingGateway(),
        lifecycle=lifecycle or Lifecycle(),
        clock=clock,
    )


def _retail_billing(db, due: datetime, *, interval: str = "quarterly", amount: int = 3000):
    tag = uuid.uuid4().hex[:8]
    owner = User(username=f"a0-ren-{tag}", email=f"a0-ren-{tag}@example.com")
    owner.set_password("password123")
    db.add(owner)
    db.flush()
    account = BillingAccountDAO.ensure_client_account(db, owner.id)
    service = Service(name=f"a0-ren-{tag}", owner_user=owner, service_type=ServiceType.HTTP_PROXY,
                      status=ServiceStatus.ACTIVE)
    db.add(service)
    db.flush()
    billing = ServiceBilling(
        service_id=service.id,
        reseller_id=None,
        billing_account_id=account.id,
        setup_price_cents=0,
        monthly_price_cents=amount,
        currency="USD",
        next_charge_at=due,
        billing_anchor_day=due.day,
        cycle_interval=interval,
        interval_months=interval_months(interval),
        status=ServiceBillingStatus.ACTIVE,
    )
    db.add(billing)
    db.commit()
    return billing, account


def _reseller_billing(db, due: datetime, *, months: int | None):
    tag = uuid.uuid4().hex[:8]
    reseller = Reseller(user=User(username=f"a0-rr-{tag}", email=f"a0-rr-{tag}@example.com", is_reseller=True))
    db.add(reseller)
    db.flush()
    owner = User(username=f"a0-rc-{tag}", email=f"a0-rc-{tag}@example.com", reseller_id=reseller.id)
    service = Service(name=f"a0-rs-{tag}", owner_user=owner, service_type=ServiceType.HTTP_PROXY,
                      status=ServiceStatus.ACTIVE)
    db.add(service)
    db.flush()
    billing = ServiceBilling(
        service_id=service.id, reseller_id=reseller.id, setup_price_cents=0, monthly_price_cents=1500,
        currency="USD", next_charge_at=due, billing_anchor_day=due.day, interval_months=months,
        status=ServiceBillingStatus.ACTIVE,
    )
    db.add(billing)
    db.commit()
    return billing


# Item 5: renewals respect the billing interval.


def test_advance_months_keeps_anchor_across_short_months():
    start = datetime(2027, 1, 31, 12, tzinfo=timezone.utc)
    first = advance_months(start, 3, 31)
    assert first == datetime(2027, 4, 30, 12, tzinfo=timezone.utc)
    assert advance_months(first, 3, 31) == datetime(2027, 7, 31, 12, tzinfo=timezone.utc)
    assert advance_months(start, 12, 31) == datetime(2028, 1, 31, 12, tzinfo=timezone.utc)
    assert advance_months(datetime(2028, 2, 29, tzinfo=timezone.utc), 12, 29) == datetime(2029, 2, 28, tzinfo=timezone.utc)
    assert CommerceRecurringService.next_due_at(start, "semiannually") == datetime(2027, 7, 31, 12, tzinfo=timezone.utc)
    assert interval_months(None) == 1
    with pytest.raises(ValueError):
        interval_months("unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("months,expected", [(None, datetime(2027, 2, 28, 12, tzinfo=timezone.utc)),
                                             (3, datetime(2027, 4, 30, 12, tzinfo=timezone.utc)),
                                             (12, datetime(2028, 1, 31, 12, tzinfo=timezone.utc))])
async def test_reseller_renewal_advances_by_interval(db_session, months, expected):
    due = datetime(2027, 1, 31, 12, tzinfo=timezone.utc)
    billing = _reseller_billing(db_session, due, months=months)
    result = await _worker(Clock(due)).process_batch(db_session, owner="t")
    assert result["paid"] == 1
    db_session.refresh(billing)
    assert _utc(billing.next_charge_at) == expected


# Item 6: retail rows processed by the worker behind billing_retail_renewals_enabled.


@pytest.mark.asyncio
async def test_retail_rows_skipped_while_setting_off(db_session):
    due = datetime(2027, 3, 1, 9, tzinfo=timezone.utc)
    billing, _ = _retail_billing(db_session, due)
    result = await _worker(Clock(due), retail=False).process_batch(db_session, owner="t")
    assert result["processed"] == 0
    assert db_session.query(BillingCycle).filter_by(service_billing_id=billing.id).count() == 0


@pytest.mark.asyncio
async def test_retail_cycle_invoice_left_open_emailed_then_settled(db_session):
    due = datetime(2027, 1, 31, 9, tzinfo=timezone.utc)
    clock = Clock(due)
    billing, account = _retail_billing(db_session, due, interval="quarterly")
    gateway = PayingGateway()
    worker = _worker(clock, gateway=gateway, retail=True)

    first = await worker.process_batch(db_session, owner="t")
    assert first["pending_action"] == 1
    assert gateway.calls == []
    cycle = db_session.execute(select(BillingCycle).where(BillingCycle.service_billing_id == billing.id)).scalar_one()
    invoice = db_session.get(Invoice, cycle.invoice_id)
    assert invoice.purpose == InvoicePurpose.CYCLE_CHARGE
    assert invoice.status == InvoiceStatus.OPEN
    assert (invoice.reseller_id, invoice.billing_account_id) == (None, account.id)
    assert invoice.amount_cents == 3000
    db_session.refresh(billing)
    assert billing.status == ServiceBillingStatus.PAST_DUE
    assert billing.last_failure_code == "awaiting_client_payment"
    emails = db_session.execute(
        select(EmailMessage).where(EmailMessage.idempotency_key == f"invoice:{invoice.id}:created")
    ).scalars().all()
    assert len(emails) == 1

    clock.current = datetime(2027, 2, 2, 9, tzinfo=timezone.utc)
    await worker.process_batch(db_session, owner="t")
    assert db_session.query(Invoice).filter_by(service_id=billing.service_id).count() == 1
    assert db_session.query(EmailMessage).filter_by(idempotency_key=f"invoice:{invoice.id}:created").count() == 1

    record_manual_payment(db_session, invoice.id, reason="client paid", actor_user_id=None)
    db_session.commit()
    db_session.refresh(cycle)
    db_session.refresh(billing)
    assert cycle.state == BillingCycleState.PAID
    assert billing.status == ServiceBillingStatus.ACTIVE
    assert _utc(billing.next_charge_at) == datetime(2027, 4, 30, 9, tzinfo=timezone.utc)
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.ACTIVE


# Item 7: end-of-period cancellation.


@pytest.mark.asyncio
async def test_voided_retail_renewal_invoice_goes_to_manual_review(db_session):
    due = datetime(2027, 1, 10, 9, tzinfo=timezone.utc)
    clock = Clock(due)
    billing, _ = _retail_billing(db_session, due)
    worker = _worker(clock, retail=True)
    await worker.process_batch(db_session, owner="t")
    cycle = db_session.execute(select(BillingCycle).where(BillingCycle.service_billing_id == billing.id)).scalar_one()
    db_session.get(Invoice, cycle.invoice_id).status = InvoiceStatus.VOID
    db_session.commit()
    clock.current = datetime(2027, 1, 12, 9, tzinfo=timezone.utc)
    result = await worker.process_batch(db_session, owner="t")
    assert result["manual_review"] == 1
    db_session.refresh(billing)
    db_session.refresh(cycle)
    assert (cycle.state, billing.status) == (BillingCycleState.MANUAL_REVIEW, ServiceBillingStatus.MANUAL_REVIEW)
    assert billing.last_failure_code == "invoice_void"


@pytest.mark.asyncio
@pytest.mark.parametrize("retail_enabled", [False, True])
async def test_end_of_cycle_cancel_terminates_at_period_end(db_session, retail_enabled):
    due = datetime(2027, 5, 15, 9, tzinfo=timezone.utc)
    billing, account = _retail_billing(db_session, due, interval="monthly")
    outcome = await CommerceLifecycleService.request_cancellation(
        db_session, billing.service_id, account, when="end_of_cycle"
    )
    db_session.commit()
    db_session.refresh(billing)
    assert _utc(billing.cancel_at) == due
    assert billing.status == ServiceBillingStatus.ACTIVE
    assert outcome["cancel_at"] is not None
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.ACTIVE

    clock = Clock(datetime(2027, 5, 1, 9, tzinfo=timezone.utc))
    lifecycle = Lifecycle()
    worker = _worker(clock, lifecycle=lifecycle, retail=retail_enabled)
    assert (await worker.process_batch(db_session, owner="t"))["processed"] == 0

    clock.current = due
    result = await worker.process_batch(db_session, owner="t")
    assert result["cancelled"] == 1
    db_session.refresh(billing)
    service = db_session.get(Service, billing.service_id)
    assert service.status == ServiceStatus.TERMINATED
    assert lifecycle.suspended == [service.id]
    assert billing.status == ServiceBillingStatus.CANCELLED
    assert db_session.query(Invoice).filter_by(service_id=billing.service_id).count() == 0

    clock.current = datetime(2027, 7, 1, tzinfo=timezone.utc)
    assert (await worker.process_batch(db_session, owner="t"))["processed"] == 0
