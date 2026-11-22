"""Job ``nonpayment_suspension`` and the unsuspension after payment (P2-L, design 10.4).

The runs go through ``runner.run_job`` on a database of the test's own.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.models.billing_jobs import (
    BillingJobAction,
    BillingJobActionStatus,
    BillingJobMode,
    BillingJobRun,
    BillingJobState,
    NonpaymentPolicyScope,
)
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
from app.services.billing.jobs import runner
from app.services.billing.jobs.suspension import NonpaymentSuspensionJob
from app.services.service_lifecycle import ServiceLifecycleError
from tests.services.p2l_support import (
    NOW,
    enable_templates,
    engine_v2,
    go_live,
    job_database,
    make_account,
    make_billing,
    make_cycle_invoice,
    make_reseller,
    make_run,
    make_service,
    run_job,
    set_policy,
)

JOB = "nonpayment_suspension"


class FakeLifecycle:
    def __init__(self, fail: bool = False, on_suspend=None):
        self.fail = fail
        self.on_suspend = on_suspend
        self.calls: list[tuple[str, int]] = []

    async def suspend(self, db, service, *, reason):
        self.calls.append(("suspend", service.id))
        if self.on_suspend is not None:
            self.on_suspend(db, service)
        if self.fail:
            raise ServiceLifecycleError("power off failed")
        if service.status in {ServiceStatus.SUSPENDED, ServiceStatus.TERMINATED}:
            return False
        service.status = ServiceStatus.SUSPENDED
        db.flush()
        return True

    async def unsuspend(self, db, service, *, reason):
        self.calls.append(("unsuspend", service.id))
        if service.status != ServiceStatus.SUSPENDED:
            return False
        service.status = ServiceStatus.ACTIVE
        db.flush()
        return True


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    yield from job_database(tmp_path, monkeypatch)


@pytest.fixture
def env(db_session, monkeypatch):
    monkeypatch.setattr(plugin_events, "emit", lambda *args, **kwargs: None)
    engine_v2(db_session)
    enable_templates(db_session)
    set_policy(db_session, suspend_after_days=5)
    account = make_account(db_session)
    billing = make_billing(db_session, account, make_service(db_session, account))
    lifecycle = FakeLifecycle()
    return account, billing, lifecycle, NonpaymentSuspensionJob(lambda: lifecycle)


def _key(billing, invoice) -> str:
    return f"service:{billing.service_id}:suspend:invoice:{invoice.id}"


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
    first = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    second = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=10))

    outcomes = run_job(db_session, job, make_run(JOB))

    assert outcomes == {_key(billing, first): "done"}
    assert lifecycle.calls == [("suspend", billing.service_id)]
    assert _service(db_session, billing).status == ServiceStatus.SUSPENDED
    row = db_session.get(ServiceBilling, billing.id)
    assert row.status == ServiceBillingStatus.SUSPENDED_NONPAYMENT
    assert row.last_failure_code == "nonpayment"
    claim = db_session.query(BillingJobAction).one()
    assert claim.subject_key == _key(billing, first)
    assert claim.status == BillingJobActionStatus.DONE
    assert claim.detail["suspended"] is True and claim.detail["invoice_id"] == first.id
    assert second.id != first.id


def test_the_claim_and_billing_status_commit_before_the_lifecycle_call(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    seen = []

    def check_committed(db, service):
        with sessionmaker(bind=db_session.get_bind())() as other:
            claim = other.query(BillingJobAction).one()
            seen.append((claim.status, other.get(ServiceBilling, billing.id).status))

    lifecycle.on_suspend = check_committed
    assert run_job(db_session, job, make_run(JOB)) == {_key(billing, invoice): "done"}
    assert seen == [(BillingJobActionStatus.CLAIMED, ServiceBillingStatus.SUSPENDED_NONPAYMENT)]


def test_policy_null_or_disabled_never_suspends(db_session, env):
    _, billing, lifecycle, job = env
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    set_policy(db_session, suspend_after_days=None)
    assert job.plan(db_session, make_run(JOB), billing.id) is None
    # No policy suspends, so a live run is skipped as a whole.
    assert run_job(db_session, job, make_run(JOB), status="skipped") == {}
    set_policy(db_session, suspend_after_days=5, enabled=False)
    assert job.plan(db_session, make_run(JOB), billing.id) is None
    assert run_job(db_session, job, make_run(JOB), status="skipped") == {}
    assert lifecycle.calls == []


def test_product_policy_overrides_the_default(db_session, env):
    _, billing, lifecycle, job = env
    billing.product_id = 77
    db_session.commit()
    set_policy(db_session, scope=NonpaymentPolicyScope.PRODUCT, product_id=77, suspend_after_days=30)
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=10))
    assert run_job(db_session, job, make_run(JOB)) == {}
    assert lifecycle.calls == []


def test_not_before_the_watermark_but_the_next_invoice_can_trigger(db_session, env):
    _, billing, lifecycle, job = env
    old = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=60))
    run = make_run(JOB, watermark=NOW - timedelta(days=20))
    assert run_job(db_session, job, run) == {}
    newer = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=10))
    assert run_job(db_session, job, run) == {_key(billing, newer): "done"}
    claim = db_session.query(BillingJobAction).one()
    assert claim.subject_key.endswith(f":invoice:{newer.id}")
    assert old.id != newer.id


def test_a_policy_change_moves_the_watermark(db_session, env):
    _, billing, lifecycle, job = env
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=7))  # trigger NOW - 2
    go_live(db_session, job, NOW - timedelta(days=4))
    set_policy(db_session, suspend_after_days=6)  # trigger NOW - 1
    # The policy counts as changed at this run's now: the trigger is before it.
    assert run_job(db_session, job, make_run(JOB), arm=False) == {}
    state = db_session.get(BillingJobState, JOB)
    assert state.config_since.replace(tzinfo=NOW.tzinfo) == NOW
    assert lifecycle.calls == []


def test_a_template_disabled_after_going_live_skips_the_run_and_suspends_nobody(db_session, env):
    _, billing, lifecycle, job = env
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=7))
    go_live(db_session, job, NOW - timedelta(days=4))
    enable_templates(db_session, "service_suspended_nonpayment", enabled=False)

    assert run_job(db_session, job, make_run(JOB), arm=False, status="skipped") == {}

    row = db_session.query(BillingJobRun).filter_by(job=JOB, status="skipped").one()
    assert row.error == "preconditions not met: the service_suspended_nonpayment email template is not enabled"
    assert db_session.get(BillingJobState, JOB).mode == BillingJobMode.LIVE
    assert lifecycle.calls == [] and db_session.query(BillingJobAction).count() == 0
    assert _service(db_session, billing).status == ServiceStatus.ACTIVE


def test_switching_the_job_off_mid_run_finishes_the_item_in_hand_and_stops(db_session, env):
    account, billing, lifecycle, job = env
    other = make_billing(db_session, account, make_service(db_session, account))
    first = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    make_cycle_invoice(db_session, other, due_at=NOW - timedelta(days=20))

    def switch_off(db, service):
        with sessionmaker(bind=db_session.get_bind())() as session:
            runner.change_mode(session, JOB, "off", now=NOW, actor_user_id=None)
            session.commit()

    lifecycle.on_suspend = switch_off
    assert run_job(db_session, job, make_run(JOB), status="aborted") == {_key(billing, first): "done"}
    assert lifecycle.calls == [("suspend", billing.service_id)]
    claim = db_session.query(BillingJobAction).one()
    assert claim.status == BillingJobActionStatus.DONE
    assert db_session.get(ServiceBilling, other.id).status == ServiceBillingStatus.ACTIVE


def test_the_forecast_lists_coming_suspensions_without_writing(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=2))  # trigger NOW + 3
    forecast = runner.forecast(db_session, JOB, now=NOW, days=10)
    assert [(row["subject_key"], row["trigger_at"]) for row in forecast] == [
        (_key(billing, invoice), (NOW + timedelta(days=3)).isoformat())
    ]
    assert db_session.query(BillingJobAction).count() == 0
    assert db_session.get(ServiceBilling, billing.id).status == ServiceBillingStatus.ACTIVE


def test_reseller_billings_are_never_touched(db_session, env):
    _, _, lifecycle, job = env
    reseller = make_reseller(db_session)
    set_policy(db_session, scope=NonpaymentPolicyScope.RESELLER_DEFAULT, suspend_after_days=1)
    service = make_service(db_session, None)
    reseller_billing = make_billing(db_session, None, service, reseller_id=reseller.id)
    invoice = make_cycle_invoice(db_session, reseller_billing, due_at=NOW - timedelta(days=20))
    run = make_run(JOB)
    assert reseller_billing.id not in set(job.candidates(db_session, run))
    assert job.plan(db_session, run, reseller_billing.id) is None
    assert run_job(db_session, job, run) == {}
    assert _service(db_session, reseller_billing).status == ServiceStatus.ACTIVE
    assert invoice.id


def test_failed_suspension_needs_review_and_is_not_retried(db_session, env):
    _, billing, lifecycle, job = env
    lifecycle.fail = True
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    assert run_job(db_session, job, make_run(JOB)) == {_key(billing, invoice): "failed"}
    claim = db_session.query(BillingJobAction).one()
    assert claim.status == BillingJobActionStatus.NEEDS_REVIEW
    assert "power off failed" in claim.detail["error"]
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_suspend_failed").count() == 1
    assert _service(db_session, billing).status == ServiceStatus.ACTIVE
    lifecycle.fail = False
    run_job(db_session, job, make_run(JOB, now=NOW + timedelta(hours=1)))
    assert lifecycle.calls == [("suspend", billing.service_id)]


def test_paid_between_plan_and_execute_skips(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    run = make_run(JOB)
    action = job.plan(db_session, run, billing.id)
    db_session.get(Invoice, invoice.id).status = InvoiceStatus.PAID
    db_session.commit()
    assert job.execute(db_session, run, action) == "skipped:paid"
    db_session.rollback()
    assert lifecycle.calls == [] and db_session.query(BillingJobAction).count() == 0


def test_money_received_between_plan_and_execute_skips(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    run = make_run(JOB)
    action = job.plan(db_session, run, billing.id)
    # The money is recorded but the status has not moved yet.
    db_session.add(Payment(
        invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.SUCCEEDED,
        amount_cents=invoice.amount_cents, refunded_cents=0, currency="USD", payment_metadata={},
    ))
    db_session.commit()
    assert job.execute(db_session, run, action) == "skipped:paid"
    db_session.rollback()
    assert lifecycle.calls == [] and db_session.query(BillingJobAction).count() == 0


def test_payment_unsuspends_after_commit(db_session, env, monkeypatch):
    from app.services.service_lifecycle import ServiceLifecycle

    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    run_job(db_session, job, make_run(JOB))

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


def _pay_elsewhere(db_session, invoice_id):
    with sessionmaker(bind=db_session.get_bind(), autoflush=False)() as other:
        _pay(other, other.get(Invoice, invoice_id))


def _emails(db_session, event):
    from app.models.commerce_email import EmailMessage

    return db_session.query(EmailMessage).filter_by(event=event).count()


def test_payment_during_the_provider_call_lifts_the_suspension(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    lifecycle.on_suspend = lambda db, service: _pay_elsewhere(db_session, invoice.id)

    assert run_job(db_session, job, make_run(JOB)) == {_key(billing, invoice): "done"}

    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.PAID
    assert lifecycle.calls == [("suspend", billing.service_id), ("unsuspend", billing.service_id)]
    assert _service(db_session, billing).status == ServiceStatus.ACTIVE
    assert db_session.get(ServiceBilling, billing.id).status == ServiceBillingStatus.ACTIVE
    claim = db_session.query(BillingJobAction).one()
    assert claim.status == BillingJobActionStatus.DONE and claim.detail["suspended"] is True
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_unsuspended").count() == 1
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_unsuspend_skipped").count() == 0
    assert _emails(db_session, "service_suspended_nonpayment") == 0
    # Nothing is left for a later run.
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(hours=1))) == {}


def test_payment_after_the_service_is_suspended_but_before_the_claim_is_done_lifts_it(db_session, env, monkeypatch):
    from app.services.billing.jobs import suspension

    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    real_finish = suspension.NonpaymentSuspensionJob._finish_under_lock

    def pay_first(db, *args):
        # The service is suspended and committed; the claim is still claimed.
        assert _service(db_session, billing).status == ServiceStatus.SUSPENDED
        _pay_elsewhere(db_session, invoice.id)
        return real_finish(db, *args)

    monkeypatch.setattr(suspension.NonpaymentSuspensionJob, "_finish_under_lock", staticmethod(pay_first))
    assert run_job(db_session, job, make_run(JOB)) == {_key(billing, invoice): "done"}
    assert _service(db_session, billing).status == ServiceStatus.ACTIVE
    # The payment left the in-flight suspension to the job, without a skip for staff.
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_unsuspend_skipped").count() == 0
    assert _emails(db_session, "service_suspended_nonpayment") == 0


def test_payment_during_the_provider_call_keeps_the_suspension_while_another_cycle_is_unpaid(db_session, env):
    _, billing, lifecycle, job = env
    first = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    second = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=10))
    lifecycle.on_suspend = lambda db, service: _pay_elsewhere(db_session, first.id)

    assert run_job(db_session, job, make_run(JOB)) == {_key(billing, first): "done"}

    assert _service(db_session, billing).status == ServiceStatus.SUSPENDED
    assert db_session.get(ServiceBilling, billing.id).status == ServiceBillingStatus.SUSPENDED_NONPAYMENT
    assert lifecycle.calls == [("suspend", billing.service_id)]
    # The suspension stands for the other cycle, so its email is sent.
    assert _emails(db_session, "service_suspended_nonpayment") == 1
    # Paying the second invoice lifts it through the payment path.
    _pay(db_session, db_session.get(Invoice, second.id))
    assert _service(db_session, billing).status == ServiceStatus.ACTIVE


def test_another_unpaid_invoice_keeps_the_service_suspended(db_session, env):
    _, billing, lifecycle, job = env
    first = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=10))
    run_job(db_session, job, make_run(JOB))
    _pay(db_session, db_session.get(Invoice, first.id))
    assert _service(db_session, billing).status == ServiceStatus.SUSPENDED
    assert db_session.get(ServiceBilling, billing.id).status == ServiceBillingStatus.SUSPENDED_NONPAYMENT


def test_staff_suspension_since_is_left_for_staff(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    run_job(db_session, job, make_run(JOB))
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
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    assert run_job(db_session, job, make_run(JOB)) == {}
    _pay(db_session, db_session.get(Invoice, invoice.id))
    assert _service(db_session, billing).status == ServiceStatus.SUSPENDED


def test_rolled_back_settlement_does_not_unsuspend(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    run_job(db_session, job, make_run(JOB))
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
    assert job.live_preconditions(db_session) == []
    enable_templates(db_session, "service_suspended_nonpayment", enabled=False)
    assert job.live_preconditions(db_session) == [
        "the service_suspended_nonpayment email template is not enabled"
    ]


def test_live_is_refused_while_the_suspension_template_is_disabled_but_dry_run_is_allowed(db_session, env):
    from tests.services.p2l_support import refused_live

    _, billing, lifecycle, job = env
    # Trigger NOW - 1, the dry run's own start, so the dry run plans it.
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=6))
    enable_templates(db_session, "service_suspended_nonpayment", enabled=False)
    assert refused_live(db_session, job, NOW) == (
        "preconditions not met: the service_suspended_nonpayment email template is not enabled"
    )
    # The dry run planned the suspension and changed nothing.
    from tests.services.p2l_support import run_items

    row = db_session.query(BillingJobRun).filter_by(job=JOB, status="complete").one()
    assert getattr(row.mode, "value", row.mode) == "dry_run"
    assert list(run_items(db_session, row.id).values()) == ["planned"]
    assert lifecycle.calls == [] and db_session.query(BillingJobAction).count() == 0
    assert _service(db_session, billing).status == ServiceStatus.ACTIVE
