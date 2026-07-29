from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import Settings
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    NotificationOutbox,
    Payment,
    PaymentStatus,
    Reseller,
    ResellerNonpaymentPolicy,
    ServiceBilling,
    ServiceBillingStatus,
)
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User
from app.services.invoice_service import InvoiceService
from app.services.notification_service import (
    NotificationEvent,
    NotificationOutboxSender,
    NotificationOutboxStatus,
    NotificationService,
)
from app.services.payments.orchestrator import (
    InvoiceFundingResult,
    PaymentOrchestrator,
)
from app.services.recurring_billing_service import RecurringBillingService


class MutableClock:
    def __init__(self, current):
        self.current = current

    def __call__(self):
        return self.current


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class FakeGateway:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def fund_invoice(self, db, reseller, invoice):
        outcome = self.outcomes.pop(0)
        self.calls.append(invoice.id)
        remaining = invoice.amount_cents - InvoiceService.allocated_cents(
            db, invoice.id
        )
        payment = Payment(
            invoice_id=invoice.id,
            gateway="fake",
            amount_cents=remaining,
            currency=invoice.currency,
            external_ref=f"fake:{invoice.id}:{len(self.calls)}",
        )
        if outcome == "success":
            payment.status = PaymentStatus.SUCCEEDED
            payment.processed_at = datetime.now(timezone.utc)
        elif outcome == "pending":
            payment.status = PaymentStatus.PENDING
            invoice.status = InvoiceStatus.PENDING_ACTION
        elif outcome == "raise":
            raise RuntimeError("simulated gateway interruption")
        else:
            payment.status = PaymentStatus.FAILED
            payment.failure_code = "declined"
            payment.failure_message = "unsafe provider detail"
        db.add(payment)
        db.flush()
        if outcome == "success":
            InvoiceService.mark_paid_if_fully_allocated(db, invoice.id)
        allocated = InvoiceService.allocated_cents(db, invoice.id)
        return InvoiceFundingResult(
            funded=allocated == invoice.amount_cents,
            allocated_cents=allocated,
            remaining_cents=max(0, invoice.amount_cents - allocated),
            pending_action=outcome == "pending",
            payment_id=payment.id,
        )


class FakeLifecycle:
    def __init__(self):
        self.suspended = []
        self.unsuspended = []

    async def suspend(self, db, service, *, reason):
        if service.status in {ServiceStatus.SUSPENDED, ServiceStatus.TERMINATED}:
            return False
        service.status = ServiceStatus.SUSPENDED
        self.suspended.append(service.id)
        db.flush()
        return True

    async def unsuspend(self, db, service, *, reason):
        if service.status != ServiceStatus.SUSPENDED:
            return False
        service.status = ServiceStatus.ACTIVE
        self.unsuspended.append(service.id)
        db.flush()
        return True


def _config(**overrides):
    values = {
        "database_url": "sqlite://",
        "recurring_billing_retry_days": "1,3,5",
        "recurring_billing_grace_days": 7,
        "recurring_billing_worker_interval_seconds": 1,
        "recurring_billing_worker_lease_seconds": 30,
        "recurring_billing_worker_batch_size": 100,
        "notification_worker_interval_seconds": 1,
        "notification_worker_lease_seconds": 30,
        "notification_worker_batch_size": 100,
    }
    values.update(overrides)
    return Settings(**values)


def _reseller(db, suffix, *, policy=ResellerNonpaymentPolicy.BLOCK_NEW):
    reseller = Reseller(
        user=User(
            username=f"cycle-reseller-{suffix}",
            email=f"cycle-reseller-{suffix}@example.com",
            is_reseller=True,
        ),
        nonpayment_policy=policy,
    )
    db.add(reseller)
    db.flush()
    return reseller


def _billing(db, reseller, suffix, due, *, amount=1500):
    owner = User(
        username=f"cycle-client-{suffix}",
        email=f"cycle-client-{suffix}@example.com",
        reseller_id=reseller.id,
    )
    service = Service(
        name=f"service-{suffix}",
        owner_user=owner,
        service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.ACTIVE,
    )
    db.add(service)
    db.flush()
    billing = ServiceBilling(
        service_id=service.id,
        reseller_id=reseller.id,
        setup_price_cents=0,
        monthly_price_cents=amount,
        currency="USD",
        next_charge_at=due,
        billing_anchor_day=due.day,
        status=ServiceBillingStatus.ACTIVE,
    )
    db.add(billing)
    db.flush()
    return billing


def _service(config, gateway, lifecycle, clock):
    return RecurringBillingService(
        config=config,
        gateway=gateway,
        lifecycle=lifecycle,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_first_due_success_and_month_end_calendar_anchor(db_session):
    due = datetime(2027, 1, 31, 12, tzinfo=timezone.utc)
    clock = MutableClock(due)
    reseller = _reseller(db_session, "month-end")
    billing = _billing(db_session, reseller, "month-end", due)
    gateway = FakeGateway(["success", "success"])
    service = _service(_config(), gateway, FakeLifecycle(), clock)

    first = await service.process_batch(db_session, owner="worker-a")
    assert first["paid"] == 1
    assert _utc(billing.next_charge_at) == datetime(
        2027, 2, 28, 12, tzinfo=timezone.utc
    )
    first_invoice_id = db_session.query(BillingCycle).one().invoice_id

    replay = await service.process_batch(db_session, owner="worker-a")
    assert replay["processed"] == 0
    assert db_session.query(Invoice).count() == 1

    clock.current = datetime(2027, 2, 28, 12, tzinfo=timezone.utc)
    await service.process_batch(db_session, owner="worker-a")
    assert _utc(billing.next_charge_at) == datetime(
        2027, 3, 31, 12, tzinfo=timezone.utc
    )
    assert db_session.query(Invoice).count() == 2
    assert gateway.calls[0] == first_invoice_id


@pytest.mark.asyncio
async def test_retry_day_1_3_5_reuses_one_invoice(db_session):
    due = datetime(2027, 4, 10, 9, tzinfo=timezone.utc)
    clock = MutableClock(due)
    reseller = _reseller(db_session, "retry")
    billing = _billing(db_session, reseller, "retry", due)
    gateway = FakeGateway(["fail", "fail", "fail", "fail"])
    service = _service(_config(), gateway, FakeLifecycle(), clock)

    await service.process_batch(db_session, owner="retry-worker")
    cycle = db_session.query(BillingCycle).one()
    invoice_id = cycle.invoice_id
    assert cycle.attempts == 1
    assert _utc(cycle.next_retry_at) == due + timedelta(days=1)

    for day, attempts, next_day in [(1, 2, 3), (3, 3, 5), (5, 4, None)]:
        clock.current = due + timedelta(days=day)
        await service.process_batch(db_session, owner="retry-worker")
        db_session.refresh(cycle)
        assert cycle.attempts == attempts
        assert cycle.invoice_id == invoice_id
        expected = due + timedelta(days=next_day) if next_day else None
        assert (
            _utc(cycle.next_retry_at) if cycle.next_retry_at else None
        ) == expected

    assert gateway.calls == [invoice_id] * 4
    assert db_session.query(Invoice).count() == 1
    assert billing.status == ServiceBillingStatus.GRACE
    assert reseller.billing_hold is True


@pytest.mark.asyncio
async def test_pending_action_waits_then_retries_same_invoice(db_session):
    due = datetime(2027, 5, 1, tzinfo=timezone.utc)
    clock = MutableClock(due)
    reseller = _reseller(db_session, "pending")
    billing = _billing(db_session, reseller, "pending", due)
    gateway = FakeGateway(["pending", "success"])
    service = _service(_config(), gateway, FakeLifecycle(), clock)

    await service.process_batch(db_session, owner="pending-worker")
    cycle = db_session.query(BillingCycle).one()
    invoice_id = cycle.invoice_id
    assert cycle.state == BillingCycleState.PENDING_ACTION

    clock.current = due + timedelta(days=1)
    await service.process_batch(db_session, owner="pending-worker")
    assert gateway.calls == [invoice_id]

    pending = db_session.query(Payment).one()
    pending.status = PaymentStatus.FAILED
    cycle.invoice.status = InvoiceStatus.OPEN
    db_session.commit()
    await service.process_batch(db_session, owner="pending-worker")
    assert gateway.calls == [invoice_id, invoice_id]
    assert cycle.state == BillingCycleState.PAID


@pytest.mark.asyncio
async def test_unresolved_pending_action_suspends_at_grace_expiry(db_session):
    due = datetime(2027, 5, 15, tzinfo=timezone.utc)
    clock = MutableClock(due)
    reseller = _reseller(db_session, "pending-expired")
    billing = _billing(db_session, reseller, "pending-expired", due)
    gateway = FakeGateway(["pending"])
    service = _service(_config(), gateway, FakeLifecycle(), clock)

    await service.process_batch(db_session, owner="pending-expired-worker")
    clock.current = due + timedelta(days=7)
    await service.process_batch(db_session, owner="pending-expired-worker")
    assert billing.status == ServiceBillingStatus.SUSPENDED_NONPAYMENT
    assert billing.service.status == ServiceStatus.SUSPENDED
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy, expected_second_suspended",
    [
        (ResellerNonpaymentPolicy.BLOCK_NEW, False),
        (ResellerNonpaymentPolicy.SUSPEND_ALL, True),
    ],
)
async def test_grace_suspension_policies(
    db_session, policy, expected_second_suspended
):
    due = datetime(2027, 6, 1, tzinfo=timezone.utc)
    clock = MutableClock(due)
    reseller = _reseller(db_session, policy.value, policy=policy)
    unpaid = _billing(db_session, reseller, f"{policy.value}-unpaid", due)
    other = _billing(
        db_session,
        reseller,
        f"{policy.value}-other",
        due + timedelta(days=20),
    )
    lifecycle = FakeLifecycle()
    gateway = FakeGateway(["fail"] * 4)
    service = _service(_config(), gateway, lifecycle, clock)

    for day in (0, 1, 3, 5, 7):
        clock.current = due + timedelta(days=day)
        await service.process_batch(db_session, owner=f"{policy.value}-worker")

    assert unpaid.service.status == ServiceStatus.SUSPENDED
    assert (
        other.service.status == ServiceStatus.SUSPENDED
    ) is expected_second_suspended
    assert unpaid.status == ServiceBillingStatus.SUSPENDED_NONPAYMENT
    assert (
        other.status == ServiceBillingStatus.SUSPENDED_NONPAYMENT
    ) is expected_second_suspended
    assert reseller.billing_hold is True


@pytest.mark.asyncio
async def test_late_payment_only_restores_billing_suspensions(db_session):
    due = datetime(2027, 7, 1, tzinfo=timezone.utc)
    clock = MutableClock(due)
    reseller = _reseller(db_session, "recover")
    unpaid = _billing(db_session, reseller, "recover-unpaid", due)
    manual = _billing(
        db_session, reseller, "recover-manual", due + timedelta(days=20)
    )
    manual.service.status = ServiceStatus.SUSPENDED
    lifecycle = FakeLifecycle()
    service = _service(
        _config(), FakeGateway(["fail"] * 4), lifecycle, clock
    )
    for day in (0, 1, 3, 5, 7):
        clock.current = due + timedelta(days=day)
        await service.process_batch(db_session, owner="recover-worker")

    cycle = db_session.query(BillingCycle).one()
    db_session.add(
        Payment(
            invoice_id=cycle.invoice_id,
            gateway="late",
            status=PaymentStatus.SUCCEEDED,
            amount_cents=cycle.amount_cents,
            currency=cycle.currency,
            external_ref=f"late:{cycle.id}",
        )
    )
    db_session.flush()
    InvoiceService.mark_paid_if_fully_allocated(db_session, cycle.invoice_id)
    db_session.commit()

    clock.current = due + timedelta(days=8)
    await service.process_batch(db_session, owner="recover-worker")
    assert unpaid.service.status == ServiceStatus.ACTIVE
    assert manual.service.status == ServiceStatus.SUSPENDED
    assert reseller.billing_hold is False


@pytest.mark.asyncio
async def test_late_payment_does_not_restore_manual_target_suspension(db_session):
    due = datetime(2027, 7, 15, tzinfo=timezone.utc)
    clock = MutableClock(due)
    reseller = _reseller(db_session, "manual-target")
    billing = _billing(db_session, reseller, "manual-target", due)
    billing.service.status = ServiceStatus.SUSPENDED
    lifecycle = FakeLifecycle()
    service = _service(
        _config(), FakeGateway(["fail"] * 4), lifecycle, clock
    )
    for day in (0, 1, 3, 5, 7):
        clock.current = due + timedelta(days=day)
        await service.process_batch(db_session, owner="manual-target-worker")

    cycle = db_session.query(BillingCycle).one()
    assert billing.status == ServiceBillingStatus.MANUAL_REVIEW
    assert lifecycle.suspended == []
    db_session.add(
        Payment(
            invoice_id=cycle.invoice_id,
            gateway="late",
            status=PaymentStatus.SUCCEEDED,
            amount_cents=cycle.amount_cents,
            currency=cycle.currency,
            external_ref=f"late-manual:{cycle.id}",
        )
    )
    db_session.flush()
    InvoiceService.mark_paid_if_fully_allocated(db_session, cycle.invoice_id)
    db_session.commit()

    clock.current = due + timedelta(days=8)
    await service.process_batch(db_session, owner="manual-target-worker")
    assert billing.service.status == ServiceStatus.SUSPENDED
    assert billing.status == ServiceBillingStatus.ACTIVE
    assert lifecycle.unsuspended == []


@pytest.mark.asyncio
async def test_terminated_service_cancels_without_invoice(db_session):
    due = datetime(2027, 8, 1, tzinfo=timezone.utc)
    clock = MutableClock(due)
    reseller = _reseller(db_session, "terminated")
    billing = _billing(db_session, reseller, "terminated", due)
    billing.service.status = ServiceStatus.TERMINATED
    gateway = FakeGateway(["success"])

    result = await _service(
        _config(), gateway, FakeLifecycle(), clock
    ).process_batch(db_session, owner="terminated-worker")
    assert result["cancelled"] == 1
    assert billing.status == ServiceBillingStatus.CANCELLED
    assert db_session.query(Invoice).count() == 0
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_zero_snapshot_is_manual_review_without_charge(db_session):
    due = datetime(2027, 8, 15, tzinfo=timezone.utc)
    clock = MutableClock(due)
    reseller = _reseller(db_session, "zero")
    billing = _billing(db_session, reseller, "zero", due, amount=0)
    gateway = FakeGateway(["success"])

    result = await _service(
        _config(), gateway, FakeLifecycle(), clock
    ).process_batch(db_session, owner="zero-worker")
    cycle = db_session.query(BillingCycle).one()
    assert result["manual_review"] == 1
    assert cycle.state == BillingCycleState.MANUAL_REVIEW
    assert cycle.invoice_id is None
    assert billing.status == ServiceBillingStatus.MANUAL_REVIEW
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_lease_and_restart_prevent_duplicate_cycle_charge(db_session):
    due = datetime(2027, 9, 1, tzinfo=timezone.utc)
    clock = MutableClock(due)
    reseller = _reseller(db_session, "lease")
    _billing(db_session, reseller, "lease", due)
    first_gateway = FakeGateway(["raise"])
    first = _service(_config(), first_gateway, FakeLifecycle(), clock)
    assert first.acquire_lease(db_session, owner="one") is True
    db_session.commit()
    assert first.acquire_lease(db_session, owner="two") is False
    db_session.rollback()

    await first.process_batch(db_session, owner="one")
    cycle = db_session.query(BillingCycle).one()
    invoice_id = cycle.invoice_id
    first.release_lease(db_session, owner="one")
    db_session.commit()

    clock.current = due + timedelta(days=1)
    restarted_gateway = FakeGateway(["success"])
    restarted = _service(
        _config(), restarted_gateway, FakeLifecycle(), clock
    )
    await restarted.process_batch(db_session, owner="restart")
    assert db_session.query(BillingCycle).count() == 1
    assert db_session.query(Invoice).count() == 1
    assert restarted_gateway.calls == [invoice_id]


class FakeTransport:
    def __init__(self, failures=0):
        self.failures = failures
        self.sent = []

    def send(self, row):
        if self.failures:
            self.failures -= 1
            raise TimeoutError("secret host detail")
        self.sent.append(row.id)


def test_outbox_dedupe_disabled_and_retry(db_session):
    reseller = _reseller(db_session, "outbox")
    first = NotificationService.enqueue(
        db_session,
        reseller=reseller,
        event=NotificationEvent.GRACE_WARNING,
        idempotency_key="outbox-once",
        data={"amount_cents": 1200, "currency": "USD"},
    )
    second = NotificationService.enqueue(
        db_session,
        reseller=reseller,
        event=NotificationEvent.GRACE_WARNING,
        idempotency_key="outbox-once",
        data={},
    )
    db_session.commit()
    assert first.id == second.id
    assert db_session.query(NotificationOutbox).count() == 1

    disabled = NotificationOutboxSender(config=_config(), clock=lambda: datetime.now(timezone.utc))
    disabled.send_batch(db_session, owner="disabled")
    assert first.status == NotificationOutboxStatus.SKIPPED

    now = datetime(2027, 10, 1, tzinfo=timezone.utc)
    clock = MutableClock(now)
    first.status = NotificationOutboxStatus.QUEUED
    first.next_attempt_at = now
    db_session.commit()
    transport = FakeTransport(failures=1)
    enabled = NotificationOutboxSender(
        config=_config(
            smtp_enabled=True,
            smtp_host="smtp.example.com",
            smtp_from="billing@example.com",
        ),
        transport=transport,
        clock=clock,
    )
    enabled.send_batch(db_session, owner="smtp")
    assert first.status == NotificationOutboxStatus.FAILED
    assert "TimeoutError" in first.error
    assert "secret host detail" not in first.error

    clock.current = first.next_attempt_at
    enabled.send_batch(db_session, owner="smtp")
    assert first.status == NotificationOutboxStatus.SENT
    assert transport.sent == [first.id]


def test_topup_notification_is_queued_once(db_session):
    reseller = _reseller(db_session, "topup")
    invoice = Invoice(
        invoice_number=990001,
        reseller_id=reseller.id,
        purpose=InvoicePurpose.CREDIT_TOPUP,
        status=InvoiceStatus.OPEN,
        amount_cents=2500,
        currency="USD",
    )
    db_session.add(invoice)
    db_session.flush()
    payment = Payment(
        invoice_id=invoice.id,
        gateway="fake",
        status=PaymentStatus.SUCCEEDED,
        amount_cents=2500,
        currency="USD",
        external_ref="topup-notification-once",
    )
    db_session.add(payment)
    db_session.flush()

    PaymentOrchestrator.finalize_success(db_session, payment)
    PaymentOrchestrator.finalize_success(db_session, payment)
    rows = db_session.query(NotificationOutbox).all()
    assert len(rows) == 1
    assert rows[0].event == NotificationEvent.TOPUP_RECEIVED


def test_worker_and_smtp_settings_are_validated():
    with pytest.raises(ValueError):
        _config(recurring_billing_retry_days="1,1,3")
    with pytest.raises(ValueError):
        _config(recurring_billing_grace_days=3)
    with pytest.raises(ValueError):
        _config(smtp_enabled=True, smtp_from="billing@example.com")
    with pytest.raises(ValueError):
        _config(
            smtp_enabled=True,
            smtp_host="smtp.example.com",
            smtp_from="billing@example.com",
            smtp_username="username-only",
        )
