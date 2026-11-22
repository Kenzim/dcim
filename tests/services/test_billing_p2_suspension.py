"""Job ``nonpayment_suspension`` and the unsuspension after payment (P2-L, design 10.4)."""

from __future__ import annotations

from datetime import timedelta

import pytest

import app.models  # noqa: F401
from app.models.billing_jobs import BillingJobAction, BillingJobActionStatus, NonpaymentPolicyScope
from app.models.commerce_audit import UserAuditEvent
from app.models.reseller import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    ServiceBilling,
    ServiceBillingStatus,
)
from app.models.server_activity import ServerActivity, ServerActivityEventType
from app.models.service import Service, ServiceStatus
from app.plugin_host import events as plugin_events
from app.services.billing import settlement
from app.services.billing.jobs.suspension import NonpaymentSuspensionJob
from app.services.service_lifecycle import ServiceLifecycleError
from tests.conftest import TestingSessionLocal
from tests.services.p2l_support import (
    NOW,
    engine_v2,
    make_account,
    make_billing,
    make_cycle_invoice,
    make_reseller,
    make_run,
    make_service,
    run_job,
    set_policy,
)


class FakeLifecycle:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    async def suspend(self, db, service, *, reason):
        self.calls.append(("suspend", service.id))
        if self.fail:
            raise ServiceLifecycleError("power off failed")
        if service.status in {ServiceStatus.SUSPENDED, ServiceStatus.TERMINATED}:
            return False
        service.status = ServiceStatus.SUSPENDED
        db.flush()
        return True


@pytest.fixture
def env(db_session, monkeypatch):
    import app.core.database as database

    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(plugin_events, "emit", lambda *args, **kwargs: None)
    engine_v2(db_session)
    set_policy(db_session, suspend_after_days=5)
    account = make_account(db_session)
    billing = make_billing(db_session, account, make_service(db_session, account))
    lifecycle = FakeLifecycle()
    return account, billing, lifecycle, NonpaymentSuspensionJob(lambda: lifecycle)


def _pay(db, invoice):
    payment = Payment(
        invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.SUCCEEDED,
        amount_cents=invoice.amount_cents, refunded_cents=0, currency=invoice.currency, payment_metadata={},
    )
    db.add(payment)
    db.flush()
    settlement.settle_invoice(db, invoice.id, payment=payment)
    settlement.commit_and_fulfil(db)


def _service(db, billing):
    db.expire_all()
    return db.get(Service, billing.service_id)


def test_suspends_on_the_earliest_unpaid_invoice_after_the_policy_days(db_session, env):
    _, billing, lifecycle, job = env
    first = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=40))
    second = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=10))

    outcomes = run_job(db_session, job, make_run("nonpayment_suspension"))

    assert outcomes == {billing.id: "done"}
    assert lifecycle.calls == [("suspend", billing.service_id)]
    assert _service(db_session, billing).status == ServiceStatus.SUSPENDED
    row = db_session.get(ServiceBilling, billing.id)
    assert row.status == ServiceBillingStatus.SUSPENDED_NONPAYMENT
    assert row.last_failure_code == "nonpayment"
    claim = db_session.query(BillingJobAction).one()
    assert claim.subject_key == f"service:{billing.service_id}:suspend:invoice:{first.id}"
    assert claim.status == BillingJobActionStatus.DONE
    assert claim.detail["suspended"] is True
    assert second.id != first.id


def test_policy_null_or_disabled_never_suspends(db_session, env):
    _, billing, lifecycle, job = env
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=40))
    set_policy(db_session, suspend_after_days=None)
    assert run_job(db_session, job, make_run("nonpayment_suspension")) == {}
    set_policy(db_session, suspend_after_days=5, enabled=False)
    assert run_job(db_session, job, make_run("nonpayment_suspension")) == {}
    assert lifecycle.calls == []


def test_product_policy_overrides_the_default(db_session, env):
    _, billing, lifecycle, job = env
    billing.product_id = 77
    db_session.commit()
    set_policy(db_session, scope=NonpaymentPolicyScope.PRODUCT, product_id=77, suspend_after_days=30)
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=10))
    assert run_job(db_session, job, make_run("nonpayment_suspension")) == {}
    assert lifecycle.calls == []


def test_not_before_the_watermark_but_the_next_invoice_can_trigger(db_session, env):
    _, billing, lifecycle, job = env
    old = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=60))
    run = make_run("nonpayment_suspension", watermark=NOW - timedelta(days=20))
    assert run_job(db_session, job, run) == {}
    newer = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=10))
    assert run_job(db_session, job, run) == {billing.id: "done"}
    claim = db_session.query(BillingJobAction).one()
    assert claim.subject_key.endswith(f":invoice:{newer.id}")
    assert old.id != newer.id


def test_reseller_billings_are_never_touched(db_session, env):
    _, _, lifecycle, job = env
    reseller = make_reseller(db_session)
    set_policy(db_session, scope=NonpaymentPolicyScope.RESELLER_DEFAULT, suspend_after_days=1)
    service = make_service(db_session, None)
    reseller_billing = make_billing(db_session, None, service, reseller_id=reseller.id)
    invoice = make_cycle_invoice(db_session, reseller_billing, due_at=NOW - timedelta(days=40))
    run = make_run("nonpayment_suspension")
    assert reseller_billing.id not in set(job.candidates(db_session, run))
    assert job.plan(db_session, run, reseller_billing.id) is None
    assert run_job(db_session, job, run) == {}
    assert _service(db_session, reseller_billing).status == ServiceStatus.ACTIVE
    assert invoice.id


def test_failed_suspension_needs_review_and_is_not_retried(db_session, env):
    _, billing, lifecycle, job = env
    lifecycle.fail = True
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=40))
    assert run_job(db_session, job, make_run("nonpayment_suspension")) == {billing.id: "failed"}
    claim = db_session.query(BillingJobAction).one()
    assert claim.status == BillingJobActionStatus.NEEDS_REVIEW
    assert "power off failed" in claim.detail["error"]
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_suspend_failed").count() == 1
    assert _service(db_session, billing).status == ServiceStatus.ACTIVE


def test_paid_between_plan_and_execute_skips(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=40))
    run = make_run("nonpayment_suspension")
    action = job.plan(db_session, run, billing.id)
    db_session.get(Invoice, invoice.id).status = InvoiceStatus.PAID
    db_session.commit()
    assert job.execute(db_session, run, action) == "skipped"
    assert lifecycle.calls == [] and db_session.query(BillingJobAction).count() == 0


def test_money_received_between_plan_and_execute_skips(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=40))
    run = make_run("nonpayment_suspension")
    action = job.plan(db_session, run, billing.id)
    # The money is recorded but the status has not moved yet.
    db_session.add(Payment(
        invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.SUCCEEDED,
        amount_cents=invoice.amount_cents, refunded_cents=0, currency="USD", payment_metadata={},
    ))
    db_session.commit()
    assert job.execute(db_session, run, action) == "skipped"
    assert lifecycle.calls == [] and db_session.query(BillingJobAction).count() == 0


def test_payment_unsuspends_after_commit(db_session, env, monkeypatch):
    from app.services.service_lifecycle import ServiceLifecycle

    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=40))
    run_job(db_session, job, make_run("nonpayment_suspension"))

    order: list[str] = []
    real_commit, real_unsuspend = db_session.commit, ServiceLifecycle.unsuspend

    def commit():
        order.append("commit")
        real_commit()

    async def unsuspend(self, db, service, *, reason):
        order.append("unsuspend")
        return await real_unsuspend(self, db, service, reason=reason)

    monkeypatch.setattr(db_session, "commit", commit)
    monkeypatch.setattr(ServiceLifecycle, "unsuspend", unsuspend)
    _pay(db_session, db_session.get(Invoice, invoice.id))
    monkeypatch.undo()

    # The settlement is committed before the lifecycle call.
    assert "unsuspend" in order and order.index("commit") < order.index("unsuspend")
    assert _service(db_session, billing).status == ServiceStatus.ACTIVE
    assert db_session.get(ServiceBilling, billing.id).status == ServiceBillingStatus.ACTIVE
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_unsuspended").count() == 1


def test_another_unpaid_invoice_keeps_the_service_suspended(db_session, env):
    _, billing, lifecycle, job = env
    first = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=40))
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=10))
    run_job(db_session, job, make_run("nonpayment_suspension"))
    _pay(db_session, db_session.get(Invoice, first.id))
    assert _service(db_session, billing).status == ServiceStatus.SUSPENDED
    assert db_session.get(ServiceBilling, billing.id).status == ServiceBillingStatus.SUSPENDED_NONPAYMENT


def test_staff_suspension_since_is_left_for_staff(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=40))
    run_job(db_session, job, make_run("nonpayment_suspension"))
    db_session.add(
        ServerActivity(
            service_id=billing.service_id, event_type=ServerActivityEventType.SERVICE,
            action="update_service_status", status="success", message="abuse", source="admin",
        )
    )
    db_session.commit()
    _pay(db_session, db_session.get(Invoice, invoice.id))
    assert _service(db_session, billing).status == ServiceStatus.SUSPENDED
    skipped = db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_unsuspend_skipped").one()
    assert skipped.event_metadata["reason"] == "status_changed_by_staff"


def test_service_suspended_before_the_job_is_never_unsuspended(db_session, env):
    _, billing, lifecycle, job = env
    service = db_session.get(Service, billing.service_id)
    service.status = ServiceStatus.SUSPENDED
    db_session.commit()
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=40))
    assert run_job(db_session, job, make_run("nonpayment_suspension")) == {}
    _pay(db_session, db_session.get(Invoice, invoice.id))
    assert _service(db_session, billing).status == ServiceStatus.SUSPENDED


def test_rolled_back_settlement_does_not_unsuspend(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=40))
    run_job(db_session, job, make_run("nonpayment_suspension"))
    payment = Payment(
        invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.SUCCEEDED,
        amount_cents=invoice.amount_cents, refunded_cents=0, currency="USD", payment_metadata={},
    )
    db_session.add(payment)
    db_session.flush()
    settlement.settle_invoice(db_session, invoice.id, payment=payment)
    db_session.rollback()
    settlement.commit_and_fulfil(db_session)
    assert _service(db_session, billing).status == ServiceStatus.SUSPENDED


def test_live_preconditions(db_session, env):
    *_, job = env
    problems = job.live_preconditions(db_session)
    assert any("service_suspended_nonpayment" in problem for problem in problems)
    assert not any("suspend_after_days" in problem for problem in problems)
