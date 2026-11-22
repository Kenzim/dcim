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
