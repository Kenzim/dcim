"""Cancellation requests (P2-L, design 10.6): request, invoices generated ahead, withdraw, the job."""

from __future__ import annotations

from datetime import timedelta

import pytest

import app.models  # noqa: F401
from app.models.billing_jobs import BillingJobAction, CancellationRequest, CancellationStatus
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    InvoiceStatus,
    ServiceBilling,
    ServiceBillingStatus,
)
from app.models.service import Service, ServiceStatus
from app.plugin_host import events as plugin_events
from app.services.billing.cancellations import (
    CancellationError,
    CancellationService,
    cancel_unpaid_invoices,
)
from app.services.billing.jobs.cancellations import CancellationsJob
from app.services.settings_store import set_setting
from tests.conftest import TestingSessionLocal
from tests.services.p2l_support import (
    NOW,
    engine_v2,
    make_account,
    make_billing,
    make_cycle_invoice,
    make_run,
    make_service,
    run_job,
)
from tests.services.test_billing_p2_suspension import FakeLifecycle


@pytest.fixture
def env(db_session, monkeypatch):
    import app.core.database as database

    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(plugin_events, "emit", lambda db, name, payload, **kw: emitted.append((name, payload)))
    engine_v2(db_session)
    account = make_account(db_session)
    billing = make_billing(db_session, account, make_service(db_session, account), next_charge_at=NOW + timedelta(days=10))
    return account, billing, emitted


def _request(db, billing, kind="end_of_period", key="k1", **values):
    request = CancellationService.request(
        db, service_id=billing.service_id, type=kind, reason="no longer needed", actor_user_id=None,
        source="client", idempotency_key=key, now=values.pop("now", NOW), **values,
    )
    db.commit()
    return request


def test_end_of_period_schedules_and_cancels_invoices_generated_ahead(db_session, env):
    _, billing, emitted = env
    current = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    ahead = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=10), status=InvoiceStatus.OPEN)

    request = _request(db_session, billing)

    assert request.status == CancellationStatus.SCHEDULED
    assert request.effective_at.replace(tzinfo=None) == (NOW + timedelta(days=10)).replace(tzinfo=None)
    db_session.expire_all()
    assert db_session.get(ServiceBilling, billing.id).cancel_at is not None
    assert db_session.get(Invoice, ahead.id).status == InvoiceStatus.VOID
    assert db_session.query(BillingCycle).filter_by(invoice_id=ahead.id).one().state == BillingCycleState.CANCELLED
    # The period the client had stays owed.
    assert db_session.get(Invoice, current.id).status == InvoiceStatus.OVERDUE
    assert ("service.cancellation_requested", {
        "service_id": billing.service_id, "request_id": request.id, "type": "end_of_period",
        "effective_at": (NOW + timedelta(days=10)).isoformat(),
    }) in [(name, payload) for name, payload in emitted]


def test_keep_leaves_invoices_generated_ahead(db_session, env):
    _, billing, _ = env
    ahead = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=10), status=InvoiceStatus.OPEN)
    _request(db_session, billing, invoice_action="keep")
    assert db_session.get(Invoice, ahead.id).status == InvoiceStatus.OPEN


def test_idempotent_and_one_open_request_per_service(db_session, env):
    _, billing, _ = env
    first = _request(db_session, billing, key="same")
    assert _request(db_session, billing, key="same").id == first.id
    with pytest.raises(CancellationError, match="already scheduled"):
        _request(db_session, billing, key="other")
    assert db_session.query(CancellationRequest).count() == 1


def test_immediate_terminates_and_completes(db_session, env):
    _, billing, _ = env
    ahead = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=10), status=InvoiceStatus.OPEN)
    request = _request(db_session, billing, kind="immediate", lifecycle=FakeLifecycle())
    assert request.status == CancellationStatus.COMPLETED
    db_session.expire_all()
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.TERMINATED
    assert db_session.get(ServiceBilling, billing.id).status == ServiceBillingStatus.CANCELLED
    assert db_session.get(Invoice, ahead.id).status == InvoiceStatus.VOID


def test_immediate_with_failed_power_off_stays_scheduled(db_session, env):
    _, billing, _ = env
    request = _request(db_session, billing, kind="immediate", lifecycle=FakeLifecycle(fail=True))
    assert request.status == CancellationStatus.SCHEDULED
    db_session.expire_all()
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.ACTIVE
    assert db_session.get(ServiceBilling, billing.id).cancel_at is not None


def test_partly_paid_invoice_ahead_goes_to_a_cancel_credit_note_or_review(db_session, env):
    from app.models.reseller import Payment, PaymentStatus

    _, billing, _ = env
    ahead = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=10), status=InvoiceStatus.OPEN)
    db_session.add(Payment(
        invoice_id=ahead.id, gateway="stripe", status=PaymentStatus.SUCCEEDED, amount_cents=400,
        refunded_cents=0, currency="USD", payment_metadata={},
    ))
    db_session.commit()
    _request(db_session, billing)
    db_session.expire_all()
    # void() refuses an invoice with money on it; it is left for a refund review.
    assert db_session.get(Invoice, ahead.id).status != InvoiceStatus.VOID
    from app.services.billing.order_refunds import list_refund_reviews

    assert any(review["invoice_id"] == ahead.id for review in list_refund_reviews(db_session))


def test_withdraw_clears_cancel_at(db_session, env):
    _, billing, _ = env
    request = _request(db_session, billing)
    withdrawn = CancellationService.withdraw(db_session, request.id, actor_user_id=None, now=NOW)
    db_session.commit()
    assert withdrawn.status == CancellationStatus.WITHDRAWN
    assert db_session.get(ServiceBilling, billing.id).cancel_at is None
    with pytest.raises(CancellationError, match="Only a scheduled"):
        CancellationService.withdraw(db_session, request.id, actor_user_id=None, now=NOW)
    # A new request can follow a withdrawn one.
    assert _request(db_session, billing, key="again").status == CancellationStatus.SCHEDULED


def test_withdraw_refused_after_effective_at(db_session, env):
    _, billing, _ = env
    request = _request(db_session, billing)
    with pytest.raises(CancellationError, match="already taken effect"):
        CancellationService.withdraw(db_session, request.id, actor_user_id=None, now=NOW + timedelta(days=11))


def test_job_completes_once_the_service_is_terminated(db_session, env):
    _, billing, _ = env
    request = _request(db_session, billing)
    job = CancellationsJob()
    later = make_run("cancellations", now=NOW + timedelta(days=11), watermark=NOW)
    assert run_job(db_session, job, later) == {}  # not terminated yet
    db_session.get(Service, billing.service_id).status = ServiceStatus.TERMINATED
    db_session.commit()
    # An invoice generated after the request for the cancelled period.
    stray = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=10), status=InvoiceStatus.OPEN)
    assert run_job(db_session, job, later) == {request.id: "done"}
    db_session.expire_all()
    assert db_session.get(CancellationRequest, request.id).status == CancellationStatus.COMPLETED
    assert db_session.get(Invoice, stray.id).status == InvoiceStatus.VOID
    assert db_session.query(BillingJobAction).filter_by(job="cancellations").count() == 1


def test_job_never_acts_before_its_watermark(db_session, env):
    _, billing, _ = env
    request = _request(db_session, billing)
    db_session.get(Service, billing.service_id).status = ServiceStatus.TERMINATED
    db_session.commit()
    run = make_run("cancellations", now=NOW + timedelta(days=30), watermark=NOW + timedelta(days=20))
    assert run_job(db_session, CancellationsJob(), run) == {}
    assert db_session.get(CancellationRequest, request.id).status == CancellationStatus.SCHEDULED


def test_legacy_engine_uses_the_cycle_cancellation_decision(db_session, env):
    _, billing, _ = env
    set_setting(db_session, "billing_invoice_engine", "legacy")
    from app.models.commerce_account import SystemSetting

    db_session.get(SystemSetting, "billing_engine_switch:invoice").value = {"mode": "legacy"}
    db_session.commit()
    ahead = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=10), status=InvoiceStatus.OPEN)
    assert cancel_unpaid_invoices(db_session, billing.service_id, from_period_start=NOW, cause="test") == [ahead.id]
    db_session.commit()
    assert db_session.get(Invoice, ahead.id).status == InvoiceStatus.VOID
