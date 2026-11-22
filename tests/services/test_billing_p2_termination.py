"""Job ``nonpayment_termination`` (P2-L, design 10.5): notice, its age, live runs, needs_review.

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
    NonpaymentPolicyScope,
    UnpaidInvoiceAction,
)
from app.models.commerce_audit import UserAuditEvent
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
from app.services.billing.jobs import termination as termination_module
from app.services.billing.jobs.suspension import NonpaymentSuspensionJob
from app.services.billing.jobs.termination import NonpaymentTerminationJob, notice_key, terminate_key
from app.services.commerce_lifecycle_service import ServiceTerminationError
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
from tests.services.test_billing_p2_suspension import FakeLifecycle

JOB = "nonpayment_termination"


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    yield from job_database(tmp_path, monkeypatch)


@pytest.fixture
def env(db_session, monkeypatch):
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(plugin_events, "emit", lambda db, name, payload, **kw: emitted.append((name, payload)))
    engine_v2(db_session)
    enable_templates(db_session)
    set_policy(db_session, suspend_after_days=5, terminate_after_days=30, termination_notice_days=7)
    account = make_account(db_session)
    billing = make_billing(db_session, account, make_service(db_session, account))
    return account, billing, emitted


def _suspend_retail(db, billing, invoice_due):
    invoice = make_cycle_invoice(db, billing, due_at=invoice_due)
    lifecycle = FakeLifecycle()
    run_job(db, NonpaymentSuspensionJob(lambda: lifecycle), make_run("nonpayment_suspension", now=invoice_due + timedelta(days=6)))
    db.expire_all()
    assert db.get(Service, billing.service_id).status == ServiceStatus.SUSPENDED
    return invoice


def _claims(db):
    return {row.subject_key: row for row in db.query(BillingJobAction).filter_by(job=JOB)}


def test_notice_then_terminate_after_the_notice_age(db_session, env):
    _, billing, emitted = env
    due = NOW - timedelta(days=24)  # notice at due + 23, termination at due + 30
    invoice = _suspend_retail(db_session, billing, due)
    job = NonpaymentTerminationJob()
    notice = notice_key(billing.service_id, invoice.id)

    assert run_job(db_session, job, make_run(JOB, now=NOW)) == {notice: "done"}
    claims = _claims(db_session)
    assert list(claims) == [notice]
    # One transaction: the runner marks the notice done at the run's now.
    assert claims[notice].status == BillingJobActionStatus.DONE
    assert claims[notice].finished_at.replace(tzinfo=NOW.tzinfo) == NOW
    assert ("service.termination_scheduled", {
        "service_id": billing.service_id,
        "terminate_at": (due + timedelta(days=30)).isoformat(),
        "reason": "nonpayment",
    }) in emitted

    # Due time reached, but the notice is only 6 days old.
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(days=6))) == {}
    # Seven days after the notice: terminated.
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(days=7))) == {
        terminate_key(billing.service_id): "done"
    }
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.TERMINATED
    row = db_session.get(ServiceBilling, billing.id)
    assert row.status == ServiceBillingStatus.CANCELLED
    terminated = _claims(db_session)[terminate_key(billing.service_id)]
    assert terminated.status == BillingJobActionStatus.DONE
    assert terminated.detail["cancelled_invoice_ids"] == []
    assert db_session.query(UserAuditEvent).filter_by(action="billing.nonpayment_terminated").count() == 1
    # keep (the default): the debt stays open.
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.OVERDUE


def test_the_terminate_claim_commits_before_the_lifecycle_call(db_session, env, monkeypatch):
    import app.services.commerce_lifecycle_service as lifecycle_module

    _, billing, _ = env
    _suspend_retail(db_session, billing, NOW - timedelta(days=24))
    job = NonpaymentTerminationJob()
    run_job(db_session, job, make_run(JOB, now=NOW))
    real = lifecycle_module.terminate_service
    seen = []

    async def checking(db, service, **kwargs):
        with sessionmaker(bind=db_session.get_bind())() as other:
            seen.append(other.query(BillingJobAction).filter_by(subject_key=terminate_key(service.id)).one().status)
        return await real(db, service, **kwargs)

    monkeypatch.setattr(lifecycle_module, "terminate_service", checking)
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(days=7))) == {
        terminate_key(billing.service_id): "done"
    }
    assert seen == [BillingJobActionStatus.CLAIMED]


def test_the_forecast_steps_day_by_day_to_the_notice_without_writing(db_session, env):
    from app.services.billing.jobs import runner

    _, billing, _ = env
    due = NOW - timedelta(days=20)  # notice at due + 23 = NOW + 3
    invoice = _suspend_retail(db_session, billing, due)
    forecast = runner.forecast(db_session, JOB, now=NOW, days=5)
    assert [(row["subject_key"], row["trigger_at"]) for row in forecast] == [
        (notice_key(billing.service_id, invoice.id), (due + timedelta(days=23)).isoformat())
    ]
    assert _claims(db_session) == {}
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.SUSPENDED


def test_no_termination_without_a_notice(db_session, env):
    _, billing, _ = env
    invoice = _suspend_retail(db_session, billing, NOW - timedelta(days=60))
    # The notice time (due + 23) is before the watermark: nothing is ever sent
    # for this invoice, so it can never terminate.
    run = make_run(JOB, now=NOW, watermark=NOW - timedelta(days=10))
    job = NonpaymentTerminationJob()
    assert run_job(db_session, job, run) == {}
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(days=60), watermark=NOW - timedelta(days=10))) == {}
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.SUSPENDED
    assert invoice.id


def test_a_dry_run_plans_the_notice_but_leaves_no_claim(db_session, env):
    _, billing, _ = env
    invoice = _suspend_retail(db_session, billing, NOW - timedelta(days=24))
    job = NonpaymentTerminationJob()
    notice = notice_key(billing.service_id, invoice.id)
    assert run_job(db_session, job, make_run(JOB, now=NOW), dry_run=True) == {notice: "planned"}
    assert _claims(db_session) == {}
    # The next live run still starts with the notice, not the termination.
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(days=1))) == {notice: "done"}
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.SUSPENDED


def test_a_disabled_warning_template_skips_the_notice_unclaimed(db_session, env):
    _, billing, _ = env
    invoice = _suspend_retail(db_session, billing, NOW - timedelta(days=24))
    job, run = NonpaymentTerminationJob(), make_run(JOB, now=NOW)
    action = job.plan(db_session, run, billing.id)
    enable_templates(db_session, "service_termination_warning", enabled=False)
    assert job.execute(db_session, run, action) == "skipped:template_disabled"
    db_session.rollback()
    assert _claims(db_session) == {}
    # Sent once the template is enabled again.
    enable_templates(db_session, "service_termination_warning")
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(hours=1))) == {
        notice_key(billing.service_id, invoice.id): "done"
    }


def test_a_policy_change_moves_the_watermark(db_session, env):
    _, billing, _ = env
    _suspend_retail(db_session, billing, NOW - timedelta(days=24))  # notice at NOW - 1
    job = NonpaymentTerminationJob()
    go_live(db_session, job, NOW - timedelta(days=2))
    set_policy(db_session, termination_notice_days=8)  # notice at NOW - 2
    assert run_job(db_session, job, make_run(JOB, now=NOW), arm=False) == {}
    assert _claims(db_session) == {}


def test_paid_before_termination_is_not_terminated(db_session, env):
    _, billing, _ = env
    invoice = _suspend_retail(db_session, billing, NOW - timedelta(days=24))
    job = NonpaymentTerminationJob()
    run_job(db_session, job, make_run(JOB, now=NOW))
    db_session.get(Invoice, invoice.id).status = InvoiceStatus.PAID
    db_session.commit()
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(days=8))) == {}
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.SUSPENDED


def test_staff_unsuspension_stops_termination(db_session, env):
    _, billing, _ = env
    _suspend_retail(db_session, billing, NOW - timedelta(days=24))
    job = NonpaymentTerminationJob()
    run_job(db_session, job, make_run(JOB, now=NOW))
    db_session.get(Service, billing.service_id).status = ServiceStatus.ACTIVE
    db_session.commit()
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(days=8))) == {}
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.ACTIVE


def test_failed_termination_needs_review_and_is_never_retried(db_session, env, monkeypatch):
    _, billing, _ = env
    _suspend_retail(db_session, billing, NOW - timedelta(days=24))
    job = NonpaymentTerminationJob()
    run_job(db_session, job, make_run(JOB, now=NOW))

    async def failing(db, service, **kwargs):
        raise ServiceTerminationError("power off failed")

    import app.services.commerce_lifecycle_service as lifecycle_module

    monkeypatch.setattr(lifecycle_module, "terminate_service", failing)
    terminate = terminate_key(billing.service_id)
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(days=8))) == {terminate: "failed"}
    claim = _claims(db_session)[terminate]
    assert claim.status == BillingJobActionStatus.NEEDS_REVIEW
    assert "power off failed" in claim.detail["error"]
    assert db_session.get(ServiceBilling, billing.id).status == ServiceBillingStatus.SUSPENDED_NONPAYMENT
    monkeypatch.undo()
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(days=9))) == {terminate: "already_done"}
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.SUSPENDED


def test_cancel_action_cancels_the_unpaid_invoices(db_session, env):
    _, billing, _ = env
    set_policy(db_session, unpaid_invoice_action_on_terminate=UnpaidInvoiceAction.CANCEL)
    invoice = _suspend_retail(db_session, billing, NOW - timedelta(days=24))
    job = NonpaymentTerminationJob()
    run_job(db_session, job, make_run(JOB, now=NOW))
    run_job(db_session, job, make_run(JOB, now=NOW + timedelta(days=7)))
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.VOID
    cycle = db_session.query(BillingCycle).filter_by(invoice_id=invoice.id).one()
    assert cycle.state == BillingCycleState.CANCELLED
    assert _claims(db_session)[terminate_key(billing.service_id)].detail["cancelled_invoice_ids"] == [invoice.id]


def test_policy_null_never_terminates(db_session, env):
    _, billing, _ = env
    set_policy(db_session, terminate_after_days=None)
    _suspend_retail(db_session, billing, NOW - timedelta(days=60))
    job = NonpaymentTerminationJob()
    assert job.plan(db_session, make_run(JOB, now=NOW), billing.id) is None
    # No policy terminates, so a live run is skipped as a whole.
    assert run_job(db_session, job, make_run(JOB, now=NOW), status="skipped") == {}


def test_reseller_service_suspended_by_the_recurring_pass(db_session, env):
    reseller = make_reseller(db_session)
    set_policy(db_session, scope=NonpaymentPolicyScope.RESELLER_DEFAULT, terminate_after_days=30)
    service = make_service(db_session, None, status=ServiceStatus.SUSPENDED)
    billing = make_billing(db_session, None, service, reseller_id=reseller.id, status=ServiceBillingStatus.SUSPENDED_NONPAYMENT)
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=24))
    job = NonpaymentTerminationJob()
    # The cycle is not marked suspended by the recurring pass: no notice.
    assert run_job(db_session, job, make_run(JOB, now=NOW)) == {}
    db_session.query(BillingCycle).filter_by(invoice_id=invoice.id).one().state = BillingCycleState.SUSPENDED
    db_session.commit()
    assert run_job(db_session, job, make_run(JOB, now=NOW)) == {notice_key(service.id, invoice.id): "done"}
    assert run_job(db_session, job, make_run(JOB, now=NOW + timedelta(days=7))) == {terminate_key(service.id): "done"}
    assert db_session.get(Service, service.id).status == ServiceStatus.TERMINATED


def test_live_preconditions(db_session, env):
    job = NonpaymentTerminationJob()
    assert job.live_preconditions(db_session) == []
    enable_templates(db_session, "service_termination_warning", enabled=False)
    assert job.live_preconditions(db_session) == ["the service_termination_warning email template is not enabled"]
    assert termination_module.JOB == JOB


@pytest.mark.parametrize("event", ["service_termination_warning", "service_terminated"])
def test_live_is_refused_while_a_termination_template_is_disabled_but_dry_run_is_allowed(db_session, env, event):
    from tests.services.p2l_support import refused_live

    _, billing, _ = env
    _suspend_retail(db_session, billing, NOW - timedelta(days=24))
    enable_templates(db_session, event, enabled=False)
    job = NonpaymentTerminationJob()
    assert refused_live(db_session, job, NOW) == f"preconditions not met: the {event} email template is not enabled"
    assert _claims(db_session) == {}
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.SUSPENDED


def test_track_setting_limits_the_job(db_session, env):
    from app.services.settings_store import set_setting

    _, billing, _ = env
    _suspend_retail(db_session, billing, NOW - timedelta(days=24))
    set_setting(db_session, "billing_job_tracks_nonpayment_termination", "reseller")
    db_session.commit()
    job = NonpaymentTerminationJob()
    assert billing.id not in set(job.candidates(db_session, make_run(JOB)))
    assert job.plan(db_session, make_run(JOB), billing.id) is None
