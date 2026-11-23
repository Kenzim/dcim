"""Fixes after the P2-L review: cancellation write-offs, withdrawal, late fee scope, dispute and balance checks."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

import app.models  # noqa: F401
from app.models.billing_jobs import CancellationStatus
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    ServiceBilling,
    ServiceBillingStatus,
)
from app.models.service import Service, ServiceStatus
from app.plugin_host import events as plugin_events
from app.services.billing.cancellations import CancellationService
from app.services.billing.jobs.cancellations import CancellationsJob
from app.services.billing.jobs.late_fees import LateFeesJob
from app.services.invoice_service import InvoiceService
from app.services.recurring_billing_service import RecurringBillingService
from app.services.settings_store import set_setting
from tests.services.p2l_support import (
    NOW,
    enable_templates,
    engine_v2,
    job_database,
    make_account,
    make_billing,
    make_cycle_invoice,
    make_run,
    make_service,
    run_job,
)
from tests.services.test_billing_p2_suspension import FakeLifecycle


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    yield from job_database(tmp_path, monkeypatch)


@pytest.fixture
def env(db_session, monkeypatch):
    monkeypatch.setattr(plugin_events, "emit", lambda *args, **kwargs: None)
    engine_v2(db_session)
    enable_templates(db_session)
    for key, value in (
        ("billing_late_fee_type", "percent"),
        ("billing_late_fee_percent_bp", 1000),
        ("billing_late_fee_days", 3),
    ):
        set_setting(db_session, key, value)
    db_session.commit()
    return make_account(db_session)


def _billing(db, account, **values):
    return make_billing(db, account, make_service(db, account), **values)


def _request(db, billing, kind="end_of_period", key="k1", source="client", now=NOW, **values):
    request = CancellationService.request(
        db, service_id=billing.service_id, type=kind, reason="no longer needed", actor_user_id=None,
        source=source, idempotency_key=key, now=now, **values,
    )
    db.commit()
    return request


def _at_due(db, billing, now):
    worker = RecurringBillingService(lifecycle=FakeLifecycle())
    row = db.get(ServiceBilling, billing.id)
    outcome = asyncio.run(worker._process_billing(db, billing=row, now=now, owner="test"))
    db.commit()
    db.expire_all()
    return outcome


def _remaining(db, invoice_id):
    db.expire_all()
    return InvoiceService.balance(db, db.get(Invoice, invoice_id)).remaining_cents


def _utc(value):
    return value.replace(tzinfo=NOW.tzinfo) if value.tzinfo is None else value


@pytest.mark.parametrize("source", ["client", "reseller", "admin"])
def test_end_of_period_on_a_past_due_billing_keeps_the_used_period_and_its_late_fee(db_session, env, source):
    due = NOW - timedelta(days=20)
    billing = _billing(db_session, env, next_charge_at=due)
    invoice = make_cycle_invoice(db_session, billing, due_at=due, amount=1000)
    assert run_job(db_session, LateFeesJob(), make_run("late_fees")) == {f"invoice:{invoice.id}:late_fee": "done"}
    assert _remaining(db_session, invoice.id) == 1100

    request = _request(db_session, db_session.get(ServiceBilling, billing.id), source=source)

    # The request takes effect now, not at the past due date.
    assert _utc(request.effective_at) == NOW
    assert _utc(db_session.get(ServiceBilling, billing.id).cancel_at) == NOW
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.OVERDUE
    assert _remaining(db_session, invoice.id) == 1100
    # The recurring pass ends the service at cancel_at; the debt stays open.
    assert _at_due(db_session, billing, NOW + timedelta(hours=1)) == "cancelled"
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.TERMINATED
    assert db_session.get(ServiceBilling, billing.id).status == ServiceBillingStatus.CANCELLED
    assert _remaining(db_session, invoice.id) == 1100
    db_session.get(Service, billing.service_id).status = ServiceStatus.TERMINATED
    db_session.commit()
    later = make_run("cancellations", now=NOW + timedelta(hours=2), watermark=NOW - timedelta(days=1))
    assert run_job(db_session, CancellationsJob(), later) == {f"cancellation:{request.id}:complete": "done"}
    assert _remaining(db_session, invoice.id) == 1100


def test_withdrawing_an_end_of_period_request_leaves_the_next_period_billable(db_session, env):
    billing = _billing(db_session, env, next_charge_at=NOW + timedelta(days=10))
    ahead = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=10), status=InvoiceStatus.OPEN)
    request = _request(db_session, billing)
    # Nothing is cancelled while the request is pending.
    assert db_session.get(Invoice, ahead.id).status == InvoiceStatus.OPEN

    CancellationService.withdraw(db_session, request.id, actor_user_id=None, now=NOW + timedelta(days=1))
    db_session.commit()

    outcome = _at_due(db_session, billing, NOW + timedelta(days=10, hours=1))
    assert outcome != "manual_review"
    row = db_session.get(ServiceBilling, billing.id)
    assert row.status != ServiceBillingStatus.MANUAL_REVIEW
    assert db_session.get(Invoice, ahead.id).status == InvoiceStatus.OPEN
    assert db_session.query(BillingCycle).filter_by(invoice_id=ahead.id).one().state != BillingCycleState.CANCELLED


def test_at_due_a_pending_end_of_period_request_terminates_and_cancels_the_period_generated_ahead(db_session, env):
    billing = _billing(db_session, env, next_charge_at=NOW + timedelta(days=10))
    ahead = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=10), status=InvoiceStatus.OPEN)
    request = _request(db_session, billing)

    # At due the cancel date comes first: the service ends, nothing is charged
    # and the invoice for the period the client will not have is voided.
    assert _at_due(db_session, billing, NOW + timedelta(days=10, hours=1)) == "cancelled"
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.TERMINATED
    assert db_session.get(Invoice, ahead.id).status == InvoiceStatus.VOID
    assert db_session.query(Payment).count() == 0
    later = make_run("cancellations", now=NOW + timedelta(days=11), watermark=NOW)
    assert run_job(db_session, CancellationsJob(), later) == {f"cancellation:{request.id}:complete": "done"}
    assert db_session.get(type(request), request.id).status == CancellationStatus.COMPLETED


def test_an_invoice_ahead_paid_while_the_request_is_pending_goes_to_refund_review(db_session, env):
    from app.services.billing.order_refunds import list_refund_reviews

    billing = _billing(db_session, env, next_charge_at=NOW + timedelta(days=10))
    ahead = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=10), status=InvoiceStatus.OPEN)
    request = _request(db_session, billing)
    db_session.add(Payment(
        invoice_id=ahead.id, gateway="stripe", status=PaymentStatus.SUCCEEDED, amount_cents=1000,
        refunded_cents=0, currency="USD", payment_metadata={},
    ))
    db_session.get(Invoice, ahead.id).status = InvoiceStatus.PAID
    db_session.get(Service, billing.service_id).status = ServiceStatus.TERMINATED
    db_session.commit()
    later = make_run("cancellations", now=NOW + timedelta(days=11), watermark=NOW)
    assert run_job(db_session, CancellationsJob(), later) == {f"cancellation:{request.id}:complete": "done"}
    assert any(review["invoice_id"] == ahead.id for review in list_refund_reviews(db_session))


def test_a_pending_request_stops_reminders_and_late_fees_for_the_period_generated_ahead(db_session, env):
    from app.services.billing.jobs.late_fees import _settings
    from app.services.billing.jobs.reminders import InvoiceReminders

    billing = _billing(db_session, env, next_charge_at=NOW + timedelta(days=2))
    ahead = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=2), status=InvoiceStatus.OPEN)
    _request(db_session, billing)
    assert InvoiceReminders()._open_reason(db_session, db_session.get(Invoice, ahead.id), for_update=False) == (
        "cancelled_period"
    )
    assert LateFeesJob()._evaluate(
        db_session, make_run("late_fees", now=NOW + timedelta(days=30)), db_session.get(Invoice, ahead.id),
        _settings(db_session), for_update=False,
    ) is None


def test_late_fees_apply_to_cycle_invoices_only(db_session, env):
    billing = _billing(db_session, env)
    cycle = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=30))
    checkout = Invoice(
        invoice_number=99001, billing_account_id=billing.billing_account_id, purpose=InvoicePurpose.ORDER_CHARGE,
        status=InvoiceStatus.OPEN, amount_cents=1000, currency="USD", due_at=NOW - timedelta(days=30),
    )
    db_session.add(checkout)
    db_session.commit()
    run = make_run("late_fees")
    assert checkout.id not in set(LateFeesJob().candidates(db_session, run))
    assert run_job(db_session, LateFeesJob(), run) == {f"invoice:{cycle.id}:late_fee": "done"}
    assert _remaining(db_session, checkout.id) == 1000
    assert _remaining(db_session, cycle.id) == 1100


def test_an_invoice_with_a_chargeback_gets_no_late_fee(db_session, env):
    from datetime import datetime, timezone

    from app.models.refund import Refund, RefundDestination, RefundReason, RefundStatus

    billing = _billing(db_session, env)
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=30))
    payment = Payment(
        invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.SUCCEEDED, amount_cents=400,
        refunded_cents=0, currency="USD", payment_metadata={},
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add(Refund(
        payment_id=payment.id, invoice_id=invoice.id, billing_account_id=invoice.billing_account_id,
        amount_cents=400, currency="USD", destination=RefundDestination.GATEWAY, gateway="stripe",
        reason=RefundReason.DISPUTE, status=RefundStatus.PENDING, idempotency_key="dispute-1",
        requested_at=datetime.now(timezone.utc), refund_metadata={},
    ))
    db_session.commit()
    assert _remaining(db_session, invoice.id) > 0
    assert run_job(db_session, LateFeesJob(), make_run("late_fees")) == {
        f"invoice:{invoice.id}:late_fee": "skipped:dispute"
    }
