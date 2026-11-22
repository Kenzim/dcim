"""Job ``nonpayment_termination``: the locked balance re-check before the provider call (P2-L review)."""

from __future__ import annotations

from datetime import timedelta

import pytest

import app.models  # noqa: F401
from app.models.reseller import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.service import Service, ServiceStatus
from app.plugin_host import events as plugin_events
from app.services.billing.jobs.suspension import NonpaymentSuspensionJob
from app.services.billing.jobs.termination import NonpaymentTerminationJob
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
    set_policy,
)
from tests.services.test_billing_p2_suspension import FakeLifecycle

JOB = "nonpayment_termination"


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    yield from job_database(tmp_path, monkeypatch)


def test_money_received_with_the_invoice_still_open_stops_the_termination(db_session, monkeypatch):
    import app.services.commerce_lifecycle_service as lifecycle_module

    monkeypatch.setattr(plugin_events, "emit", lambda *args, **kwargs: None)
    engine_v2(db_session)
    enable_templates(db_session)
    set_policy(db_session, suspend_after_days=5, terminate_after_days=30, termination_notice_days=7)
    account = make_account(db_session)
    billing = make_billing(db_session, account, make_service(db_session, account))
    due = NOW - timedelta(days=24)
    invoice = make_cycle_invoice(db_session, billing, due_at=due)
    run_job(db_session, NonpaymentSuspensionJob(lambda: FakeLifecycle()), make_run("nonpayment_suspension", now=due + timedelta(days=6)))
    job = NonpaymentTerminationJob()
    run_job(db_session, job, make_run(JOB, now=NOW))  # the notice

    calls = []
    monkeypatch.setattr(lifecycle_module, "terminate_service", lambda *args, **kwargs: calls.append(args))
    run = make_run(JOB, now=NOW + timedelta(days=7))
    action = job.plan(db_session, run, billing.id)
    assert action is not None and action.action == "terminate"
    # The money is recorded but the invoice status has not moved yet.
    db_session.add(Payment(
        invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.SUCCEEDED,
        amount_cents=invoice.amount_cents, refunded_cents=0, currency="USD", payment_metadata={},
    ))
    db_session.commit()
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.OVERDUE

    assert job.execute(db_session, run, action) == "skipped:paid"
    db_session.rollback()
    assert calls == []
    db_session.expire_all()
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.SUSPENDED
