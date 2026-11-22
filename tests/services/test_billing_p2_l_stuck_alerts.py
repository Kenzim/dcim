"""Services stuck part way through non-payment suspension: payment, settled claims and their alerts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.models  # noqa: F401
from app.models.billing_jobs import BillingJobAction, BillingJobActionStatus, BillingJobState
from app.models.commerce_audit import UserAuditEvent
from app.models.reseller import Invoice, ServiceBilling, ServiceBillingStatus
from app.models.server_activity import ServerActivity, ServerActivityEventType
from app.models.service import Service, ServiceStatus
from app.plugin_host import events as plugin_events
from app.services.alerting.billing_integrity import nonpayment_suspended_paid, nonpayment_suspension_failed
from app.services.alerting.catalogue import CATALOGUE
from app.services.billing.jobs.actions import resolve_action
from app.services.billing.jobs.nonpayment import STUCK_SERVICE_ALERTS
from app.services.billing.jobs.suspension import NonpaymentSuspensionJob
from app.services.billing.jobs.termination import NonpaymentTerminationJob
from tests.services.p2l_support import (
    NOW,
    enable_stuck_service_alerts,
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
from tests.services.test_billing_p2_suspension import FakeLifecycle, _pay

JOB = "nonpayment_suspension"
WALL_CLOCK_AFTER = datetime(2030, 1, 1, tzinfo=timezone.utc)


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
    return billing


def _detect(db, detector, now=NOW):
    return [finding.details["service_id"] for finding in detector(db, CATALOGUE[detector_key(detector)].defaults(), now)]


def detector_key(detector):
    return {
        nonpayment_suspension_failed: "billing_nonpayment_suspension_failed",
        nonpayment_suspended_paid: "billing_nonpayment_suspended_paid",
    }[detector]


def _state(db, billing):
    db.expire_all()
    return db.get(Service, billing.service_id).status, db.get(ServiceBilling, billing.id).status


def _jobs_off(db):
    for state in db.query(BillingJobState).all():
        state.mode = "off"
    db.commit()


def _failed_suspension(db, billing):
    invoice = make_cycle_invoice(db, billing, due_at=NOW - timedelta(days=20))
    lifecycle = FakeLifecycle(fail=True)
    run_job(db, NonpaymentSuspensionJob(lambda: lifecycle), make_run(JOB))
    claim = db.query(BillingJobAction).one()
    assert claim.status == BillingJobActionStatus.NEEDS_REVIEW
    return invoice, claim


def _manual_suspend(db, billing):
    db.add(ServerActivity(
        service_id=billing.service_id, event_type=ServerActivityEventType.SERVICE,
        action="suspend", status="success", message="suspended by hand after the failed power-off", source="admin",
    ))
    db.get(Service, billing.service_id).status = ServiceStatus.SUSPENDED
    db.commit()


# ---------------------------------------------------------------- failed suspension


def test_a_failed_suspension_is_alerted_and_payment_sets_the_billing_active(db_session, env):
    billing = env
    invoice, _ = _failed_suspension(db_session, billing)
    assert _state(db_session, billing) == (ServiceStatus.ACTIVE, ServiceBillingStatus.SUSPENDED_NONPAYMENT)
    assert _detect(db_session, nonpayment_suspension_failed) == [billing.service_id]
    _jobs_off(db_session)
    assert _detect(db_session, nonpayment_suspension_failed) == [billing.service_id]

    _pay(db_session, db_session.get(Invoice, invoice.id))

    assert _state(db_session, billing) == (ServiceStatus.ACTIVE, ServiceBillingStatus.ACTIVE)
    assert _detect(db_session, nonpayment_suspension_failed) == []
    assert _detect(db_session, nonpayment_suspended_paid) == []


def test_a_failed_suspension_paid_with_another_invoice_unpaid_can_be_suspended_again(db_session, env):
    billing = env
    invoice, _ = _failed_suspension(db_session, billing)
    other = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=10))
    _pay(db_session, db_session.get(Invoice, invoice.id))
    # The billing is active again, so the job can suspend for the other invoice.
    assert _state(db_session, billing) == (ServiceStatus.ACTIVE, ServiceBillingStatus.ACTIVE)
    assert _detect(db_session, nonpayment_suspension_failed) == []
    lifecycle = FakeLifecycle()
    outcome = run_job(db_session, NonpaymentSuspensionJob(lambda: lifecycle), make_run(JOB, now=NOW + timedelta(hours=1)))
    assert outcome == {f"service:{billing.service_id}:suspend:invoice:{other.id}": "done"}
    assert _state(db_session, billing) == (ServiceStatus.SUSPENDED, ServiceBillingStatus.SUSPENDED_NONPAYMENT)


# ---------------------------------------------------------------- settled, then paid


@pytest.mark.parametrize("to", ["done", "failed"])
def test_payment_lifts_a_suspension_an_admin_settled(db_session, env, to):
    billing = env
    invoice, claim = _failed_suspension(db_session, billing)
    _manual_suspend(db_session, billing)
    resolve_action(db_session, claim.id, to=to, note="suspended by hand", actor_user_id=None, now=NOW)
    db_session.commit()

    _pay(db_session, db_session.get(Invoice, invoice.id))

    assert _state(db_session, billing) == (ServiceStatus.ACTIVE, ServiceBillingStatus.ACTIVE)
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_unsuspended").count() == 1
    assert _detect(db_session, nonpayment_suspended_paid, WALL_CLOCK_AFTER) == []


def test_a_staff_change_after_the_settlement_is_left_for_staff_and_alerted(db_session, env):
    billing = env
    invoice, claim = _failed_suspension(db_session, billing)
    _manual_suspend(db_session, billing)
    resolve_action(db_session, claim.id, to="done", note="suspended by hand", actor_user_id=None, now=NOW)
    db_session.commit()
    db_session.add(ServerActivity(
        service_id=billing.service_id, event_type=ServerActivityEventType.SERVICE,
        action="update_service_status", status="success", message="abuse", source="admin",
    ))
    db_session.commit()

    _pay(db_session, db_session.get(Invoice, invoice.id))

    assert _state(db_session, billing)[0] == ServiceStatus.SUSPENDED
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_unsuspend_skipped").count() == 1
    assert _detect(db_session, nonpayment_suspended_paid) == [billing.service_id]
    _jobs_off(db_session)
    assert _detect(db_session, nonpayment_suspended_paid) == [billing.service_id]


def test_paid_while_its_claim_still_needs_review_is_alerted(db_session, env):
    billing = env
    invoice, _ = _failed_suspension(db_session, billing)
    _manual_suspend(db_session, billing)

    _pay(db_session, db_session.get(Invoice, invoice.id))

    assert _state(db_session, billing)[0] == ServiceStatus.SUSPENDED
    assert _detect(db_session, nonpayment_suspended_paid) == [billing.service_id]
    _jobs_off(db_session)
    assert _detect(db_session, nonpayment_suspended_paid) == [billing.service_id]


# ---------------------------------------------------------------- healthy states


def test_healthy_states_raise_neither_alert(db_session, env):
    billing = env
    assert _detect(db_session, nonpayment_suspension_failed) == []
    assert _detect(db_session, nonpayment_suspended_paid) == []
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    lifecycle = FakeLifecycle()
    run_job(db_session, NonpaymentSuspensionJob(lambda: lifecycle), make_run(JOB))
    # Suspended by the job with the invoice still unpaid.
    assert _state(db_session, billing) == (ServiceStatus.SUSPENDED, ServiceBillingStatus.SUSPENDED_NONPAYMENT)
    assert _detect(db_session, nonpayment_suspension_failed) == []
    assert _detect(db_session, nonpayment_suspended_paid) == []


def test_a_reseller_service_is_not_reported(db_session, env):
    from tests.services.p2l_support import make_reseller

    billing = env
    reseller = make_reseller(db_session)
    row = db_session.get(ServiceBilling, billing.id)
    row.reseller_id = reseller.id
    row.status = ServiceBillingStatus.SUSPENDED_NONPAYMENT
    db_session.commit()
    assert _detect(db_session, nonpayment_suspension_failed) == []


# ---------------------------------------------------------------- go-live


@pytest.mark.parametrize("job", [NonpaymentSuspensionJob, NonpaymentTerminationJob])
@pytest.mark.parametrize("key", STUCK_SERVICE_ALERTS)
def test_the_nonpayment_jobs_cannot_go_live_while_a_stuck_service_alert_is_off(db_session, env, job, key):
    set_policy(db_session, suspend_after_days=5, terminate_after_days=30, termination_notice_days=7)
    instance = job(lambda: FakeLifecycle()) if job is NonpaymentSuspensionJob else job()
    assert instance.live_preconditions(db_session) == []
    enable_stuck_service_alerts(db_session, key, enabled=False)
    assert instance.live_preconditions(db_session) == [f"the {key} alert is not enabled"]


def test_the_stuck_service_alerts_ship_disabled(db_session):
    assert all(CATALOGUE[key].enabled_by_default is False for key in STUCK_SERVICE_ALERTS)


# ---------------------------------------------------------------- payment lifts on state and balance


def _suspended_by_job(db, billing):
    invoice = make_cycle_invoice(db, billing, due_at=NOW - timedelta(days=20))
    run_job(db, NonpaymentSuspensionJob(lambda: FakeLifecycle()), make_run(JOB))
    assert _state(db, billing) == (ServiceStatus.SUSPENDED, ServiceBillingStatus.SUSPENDED_NONPAYMENT)
    return invoice


def test_a_partial_payment_leaving_the_invoice_overdue_does_not_lift(db_session, env):
    from app.models.reseller import Payment, PaymentStatus
    from app.services.billing import settlement

    billing = env
    invoice = _suspended_by_job(db_session, billing)
    payment = Payment(
        invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.SUCCEEDED, amount_cents=400,
        refunded_cents=0, currency=invoice.currency, payment_metadata={},
    )
    db_session.add(payment)
    db_session.flush()
    settlement.settle_invoice(db_session, invoice.id, payment=payment)
    settlement.commit_and_fulfil(db_session)
    assert _state(db_session, billing) == (ServiceStatus.SUSPENDED, ServiceBillingStatus.SUSPENDED_NONPAYMENT)
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_unsuspended").count() == 0


def test_an_admin_suspension_is_not_lifted_by_a_payment(db_session, env):
    billing = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=5))
    _manual_suspend(db_session, billing)
    _pay(db_session, db_session.get(Invoice, invoice.id))
    assert _state(db_session, billing) == (ServiceStatus.SUSPENDED, ServiceBillingStatus.ACTIVE)
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_unsuspended").count() == 0
    assert _detect(db_session, nonpayment_suspended_paid) == []


def test_a_failed_unsuspension_after_payment_is_left_for_review_not_retried(db_session, env, monkeypatch):
    from app.services.service_lifecycle import ServiceLifecycle, ServiceLifecycleError

    billing = env
    invoice = _suspended_by_job(db_session, billing)
    attempts = []

    async def failing(self, db, service, *, reason):
        attempts.append(service.id)
        raise ServiceLifecycleError("power on failed")

    monkeypatch.setattr(ServiceLifecycle, "unsuspend", failing)
    _pay(db_session, db_session.get(Invoice, invoice.id))
    assert attempts == [billing.service_id]
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_unsuspend_failed").count() == 1
    assert _state(db_session, billing)[0] == ServiceStatus.SUSPENDED
    # The next suspension run does not retry it; the alert reports it.
    run_job(db_session, NonpaymentSuspensionJob(lambda: FakeLifecycle()), make_run(JOB, now=NOW + timedelta(days=1)))
    assert attempts == [billing.service_id]
    assert _detect(db_session, nonpayment_suspended_paid, WALL_CLOCK_AFTER) == [billing.service_id]


# ---------------------------------------------------------------- go-live through the mode change


@pytest.mark.parametrize("job_class", [NonpaymentSuspensionJob, NonpaymentTerminationJob])
def test_going_live_is_refused_while_a_stuck_service_alert_is_off(db_session, env, job_class):
    from app.models.alerting import AlertTypeSetting
    from app.services.alerting.catalogue import effective_settings
    from app.services.billing.jobs import runner
    from tests.services.p2l_support import go_live

    set_policy(db_session, suspend_after_days=5, terminate_after_days=30, termination_notice_days=7)
    enable_templates(db_session)
    job = job_class(lambda: FakeLifecycle()) if job_class is NonpaymentSuspensionJob else job_class()
    enable_stuck_service_alerts(db_session, enabled=False)
    others = {key: s.enabled for key, s in effective_settings(db_session).items() if key not in STUCK_SERVICE_ALERTS}
    with pytest.raises(runner.JobModeError) as refused:
        go_live(db_session, job, NOW)
    assert all(f"the {key} alert is not enabled" in str(refused.value) for key in STUCK_SERVICE_ALERTS)
    db_session.expire_all()
    # Nothing was switched on, and no other alert changed.
    assert all(not db_session.get(AlertTypeSetting, key).enabled for key in STUCK_SERVICE_ALERTS)
    after = {key: s.enabled for key, s in effective_settings(db_session).items() if key not in STUCK_SERVICE_ALERTS}
    assert after == others


@pytest.mark.parametrize("job_class", [NonpaymentSuspensionJob, NonpaymentTerminationJob])
def test_going_live_succeeds_with_the_stuck_service_alerts_on(db_session, env, job_class):
    from tests.services.p2l_support import go_live

    set_policy(db_session, suspend_after_days=5, terminate_after_days=30, termination_notice_days=7)
    job = job_class(lambda: FakeLifecycle()) if job_class is NonpaymentSuspensionJob else job_class()
    go_live(db_session, job, NOW)
    db_session.expire_all()
    assert db_session.query(BillingJobState).filter_by(job=job.name).one().mode.value == "live"
