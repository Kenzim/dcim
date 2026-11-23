"""Fixes from the review of the settle route, the lift after payment and termination (P2-L, design 10.4, 10.5).

The runs go through ``runner.run_job`` on a database of the test's own.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import app.models  # noqa: F401
from app.models.billing_jobs import BillingJobAction
from app.models.commerce_audit import UserAuditEvent
from app.models.reseller import Invoice, ServiceBilling, ServiceBillingStatus
from app.models.server_activity import ServerActivity, ServerActivityEventType
from app.models.service import Service, ServiceStatus
from app.plugin_host import events as plugin_events
from app.services.alerting import billing_integrity
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
from tests.services.test_billing_p2_suspension import FakeLifecycle, _pay

SUSPEND = "nonpayment_suspension"
TERMINATE = "nonpayment_termination"
PARAMS = {"max_rows": 10}


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    yield from job_database(tmp_path, monkeypatch)


@pytest.fixture
def env(db_session, monkeypatch):
    monkeypatch.setattr(plugin_events, "emit", lambda *args, **kwargs: None)
    engine_v2(db_session)
    enable_templates(db_session)
    set_policy(db_session, suspend_after_days=5, terminate_after_days=30, termination_notice_days=7)
    account = make_account(db_session)
    billing = make_billing(db_session, account, make_service(db_session, account))
    lifecycle = FakeLifecycle()
    return account, billing, lifecycle, NonpaymentSuspensionJob(lambda: lifecycle)


def _state(db, billing):
    db.expire_all()
    return db.get(Service, billing.service_id).status, db.get(ServiceBilling, billing.id).status


def _staff_status(db, billing, status, *, activity_status="success", action="update_service_status"):
    """A staff status change as the admin status route records it."""
    db.add(
        ServerActivity(
            service_id=billing.service_id, event_type=ServerActivityEventType.SERVICE,
            action=action, status=activity_status, message="staff", source="admin_api",
        )
    )
    if status is not None:
        db.get(Service, billing.service_id).status = status
    db.commit()


def _audits(db, action):
    return db.query(UserAuditEvent).filter_by(action=action).count()


# ---------------------------------------------------------------- M1


def test_an_admin_hold_after_a_lifted_episode_is_not_relabelled_by_a_later_payment(db_session, env):
    _, billing, lifecycle, job = env
    first = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    run_job(db_session, job, make_run(SUSPEND))
    _pay(db_session, db_session.get(Invoice, first.id))
    assert _state(db_session, billing) == (ServiceStatus.ACTIVE, ServiceBillingStatus.ACTIVE)

    _staff_status(db_session, billing, ServiceStatus.SUSPENDED)
    second = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=10))
    _pay(db_session, db_session.get(Invoice, second.id))

    assert _state(db_session, billing) == (ServiceStatus.SUSPENDED, ServiceBillingStatus.ACTIVE)
    assert billing_integrity.nonpayment_suspended_paid(db_session, PARAMS, NOW) == []
    job_t = NonpaymentTerminationJob()
    assert billing.id not in set(job_t.candidates(db_session, make_run(TERMINATE)))
    # Renewals stay due: an active billing with its next charge set is in the pass's due set.
    assert db_session.get(ServiceBilling, billing.id).next_charge_at is not None


def test_a_staff_hold_during_the_current_episode_still_keeps_the_billing_suspended(db_session, env):
    _, billing, lifecycle, job = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=20))
    run_job(db_session, job, make_run(SUSPEND))
    _staff_status(db_session, billing, None)
    _pay(db_session, db_session.get(Invoice, invoice.id))
    assert _state(db_session, billing) == (ServiceStatus.SUSPENDED, ServiceBillingStatus.SUSPENDED_NONPAYMENT)
    assert [f.resource_id for f in billing_integrity.nonpayment_suspended_paid(db_session, PARAMS, NOW)] == [
        str(billing.service_id)
    ]
    assert db_session.query(BillingJobAction).count() == 1


# ---------------------------------------------------------------- M2


def _fail_termination(monkeypatch):
    import app.services.commerce_lifecycle_service as lifecycle_module
    from app.services.commerce_lifecycle_service import ServiceTerminationError

    real = lifecycle_module.terminate_service

    async def failing(db, service, **kwargs):
        raise ServiceTerminationError("power off failed")

    monkeypatch.setattr(lifecycle_module, "terminate_service", failing)
    return lambda: monkeypatch.setattr(lifecycle_module, "terminate_service", real)


@pytest.mark.parametrize("to", ["done", "failed"])
def test_a_second_episode_after_a_settled_termination_is_warned_and_terminated(db_session, env, monkeypatch, to):
    from app.services.billing.jobs.actions import resolve_action
    from app.services.billing.jobs.termination import notice_key
    from tests.services.test_billing_p2_suspension import _emails

    _, billing, _, job = env
    sid = billing.service_id
    term = NonpaymentTerminationJob()
    first = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=24))
    run_job(db_session, job, make_run(SUSPEND, now=NOW - timedelta(days=18)))
    assert run_job(db_session, term, make_run(TERMINATE, now=NOW)) == {notice_key(sid, first.id): "done"}
    restore = _fail_termination(monkeypatch)
    # Outcomes by value only, so the same assertions run against the per-service key.
    assert list(run_job(db_session, term, make_run(TERMINATE, now=NOW + timedelta(days=8))).values()) == ["failed"]
    restore()
    (claim,) = db_session.query(BillingJobAction).filter(
        BillingJobAction.subject_key.like(f"service:{sid}:terminate%")
    ).all()
    resolve_action(db_session, claim.id, to=to, note="checked", actor_user_id=None, now=NOW + timedelta(days=8))
    db_session.commit()
    _pay(db_session, db_session.get(Invoice, first.id))
    assert _state(db_session, billing) == (ServiceStatus.ACTIVE, ServiceBillingStatus.ACTIVE)

    # Months later the client stops paying again.
    second = make_cycle_invoice(db_session, billing, due_at=NOW + timedelta(days=40))
    run_job(db_session, job, make_run(SUSPEND, now=NOW + timedelta(days=46)))
    assert _state(db_session, billing) == (ServiceStatus.SUSPENDED, ServiceBillingStatus.SUSPENDED_NONPAYMENT)
    # The settled claim of the first episode is not this episode's: no alert.
    assert billing_integrity.nonpayment_termination_unsettled(db_session, PARAMS, NOW) == []
    assert run_job(db_session, term, make_run(TERMINATE, now=NOW + timedelta(days=63))) == {
        notice_key(sid, second.id): "done"
    }
    assert list(run_job(db_session, term, make_run(TERMINATE, now=NOW + timedelta(days=70))).values()) == ["done"]
    assert db_session.query(BillingJobAction).filter_by(subject_key=f"service:{sid}:terminate:invoice:{second.id}").one()
    assert db_session.get(Service, sid).status == ServiceStatus.TERMINATED
    assert db_session.get(ServiceBilling, billing.id).status == ServiceBillingStatus.CANCELLED
    assert _emails(db_session, "service_termination_warning") == 2
    assert _emails(db_session, "service_terminated") == 1


def test_an_old_per_service_terminate_claim_is_still_reported(db_session, env):
    from app.models.billing_jobs import BillingJobActionStatus

    _, billing, _, job = env
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=24))
    run_job(db_session, job, make_run(SUSPEND, now=NOW - timedelta(days=18)))
    db_session.add(BillingJobAction(
        job=TERMINATE, subject_key=f"service:{billing.service_id}:terminate", status=BillingJobActionStatus.FAILED,
        detail={}, claimed_at=NOW,
    ))
    db_session.commit()
    (finding,) = billing_integrity.nonpayment_termination_unsettled(db_session, PARAMS, NOW)
    assert finding.resource_id == str(billing.service_id)
