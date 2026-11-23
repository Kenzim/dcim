"""A payment racing the non-payment suspension's provider call on MariaDB (optional, design 15.5).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards. The engine is built through
``configure_engine()`` as the application's is.

The payment lands in each order around the job's step after the provider
call (claim done and invoice re-checked under the service billing lock):
committed during the call, holding the billing lock when the call ends, and
arriving while the job holds that lock. In every order the paid service ends
active, unsuspended exactly once, and the suspension email is not sent.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")


def _drop_all(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("SET foreign_key_checks = 0"))
        for table in sa.inspect(conn).get_table_names():
            conn.execute(sa.text(f"DROP TABLE `{table}`"))
        conn.execute(sa.text("SET foreign_key_checks = 1"))


@pytest.fixture(params=["default_isolation", "read_committed"])
def sessions(request, monkeypatch):
    import app.models  # noqa: F401
    from app.core.database import Base, configure_engine
    from app.models.billing_jobs import BILLING_JOB_NAMES, BillingJobState
    from app.plugin_host import events as plugin_events

    database = sa.engine.make_url(URL).database or ""
    if not database.startswith("rackflow_test"):
        pytest.fail("RACKFLOW_TEST_MARIADB_URL must name a database starting with rackflow_test")
    options = {"pool_pre_ping": True, "pool_size": 8, "max_overflow": 0}
    if request.param == "read_committed":
        options["isolation_level"] = "READ COMMITTED"
    engine = configure_engine(sa.create_engine(URL, **options))
    _drop_all(engine)
    Base.metadata.create_all(engine)
    import app.core.database as database_module

    monkeypatch.setattr(plugin_events, "emit", lambda *args, **kwargs: None)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    # Number counters, the email outbox and the deferred unsuspension open their own sessions.
    monkeypatch.setattr(database_module, "SessionLocal", factory)
    with factory() as db:
        for name in BILLING_JOB_NAMES:
            db.add(BillingJobState(job=name))
        db.commit()
    yield factory
    _drop_all(engine)
    engine.dispose()


class FakeLifecycle:
    """Suspends without a provider; ``on_suspend`` runs where the power-off would."""

    def __init__(self, on_suspend):
        self.on_suspend = on_suspend
        self.calls: list[str] = []

    async def suspend(self, db, service, *, reason):
        from app.models.service import ServiceStatus

        self.calls.append("suspend")
        self.on_suspend()
        service.status = ServiceStatus.SUSPENDED
        db.flush()
        return True

    async def unsuspend(self, db, service, *, reason):
        from app.models.service import ServiceStatus

        self.calls.append("unsuspend")
        if service.status != ServiceStatus.SUSPENDED:
            return False
        service.status = ServiceStatus.ACTIVE
        db.flush()
        return True


def _through(lifecycle):
    async def unsuspend(_self, db, service, *, reason):
        return await lifecycle.unsuspend(db, service, reason=reason)

    return unsuspend


def _seed(sessions) -> tuple[int, int, int]:
    from tests.services.p2l_support import (
        NOW,
        enable_templates,
        engine_v2,
        make_account,
        make_billing,
        make_cycle_invoice,
        make_service,
        set_policy,
    )

    with sessions() as db:
        engine_v2(db)
        enable_templates(db)
        set_policy(db, suspend_after_days=5)
        account = make_account(db)
        billing = make_billing(db, account, make_service(db, account))
        invoice = make_cycle_invoice(db, billing, due_at=NOW - timedelta(days=20), amount=1000)
        return billing.id, int(billing.service_id), invoice.id


def _pay(sessions, invoice_id: int, *, locked=None, release=None) -> None:
    """Settle the invoice in full; with ``locked`` and ``release``, hold its locks until released."""
    from app.models.reseller import Payment, PaymentStatus
    from app.services.billing import settlement
    from app.services.billing_locks import BillingLocks

    with sessions() as db:
        invoice = BillingLocks.for_invoice(db, invoice_id, reseller=True, billing_account=True)
        if locked is not None:
            locked.set()
            release.wait(10)
        payment = Payment(
            invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.SUCCEEDED,
            amount_cents=invoice.amount_cents, refunded_cents=0, currency=invoice.currency, payment_metadata={},
        )
        db.add(payment)
        db.flush()
        settlement.settle_invoice(db, invoice.id, payment=payment)
        settlement.commit_and_fulfil(db)


def _run_once(sessions, billing_id: int, lifecycle, results: list) -> None:
    """One item as the runner does it: plan, a fresh transaction, execute, then commit on ``done`` only."""
    from app.services.billing.jobs.runner import _mark_claims_done
    from app.services.billing.jobs.suspension import NonpaymentSuspensionJob
    from tests.services.p2l_support import make_run

    job, run = NonpaymentSuspensionJob(lambda: lifecycle), make_run("nonpayment_suspension")
    with sessions() as db:
        try:
            action = job.plan(db, run, billing_id)
            db.rollback()
            outcome = job.execute(db, run, action) if action is not None else "none"
            if outcome == "done":
                _mark_claims_done(db, run)
                db.commit()
            else:
                db.rollback()
            results.append(outcome)
        except Exception as exc:  # noqa: BLE001 - reported through results
            db.rollback()
            results.append(repr(exc))


def _assert_lifted(sessions, billing_id, service_id, invoice_id, lifecycle) -> None:
    from app.models.billing_jobs import BillingJobAction, BillingJobActionStatus
    from app.models.commerce_audit import UserAuditEvent
    from app.models.commerce_email import EmailMessage
    from app.models.reseller import Invoice, InvoiceStatus, ServiceBilling, ServiceBillingStatus
    from app.models.service import Service, ServiceStatus

    with sessions() as db:
        state = (
            db.get(Invoice, invoice_id).status,
            db.get(Service, service_id).status,
            db.get(ServiceBilling, billing_id).status,
        )
        assert state == (InvoiceStatus.PAID, ServiceStatus.ACTIVE, ServiceBillingStatus.ACTIVE), state
        assert db.query(BillingJobAction).one().status == BillingJobActionStatus.DONE
        assert db.query(UserAuditEvent).filter_by(action="billing.nonpayment_unsuspended").count() == 1
        assert db.query(EmailMessage).filter_by(event="service_suspended_nonpayment").count() == 0
    assert lifecycle.calls.count("suspend") == 1


def test_payment_committed_during_the_provider_call(sessions):
    billing_id, service_id, invoice_id = _seed(sessions)
    lifecycle = FakeLifecycle(lambda: _pay(sessions, invoice_id))
    results: list = []
    _run_once(sessions, billing_id, lifecycle, results)
    assert results == ["done"], results
    _assert_lifted(sessions, billing_id, service_id, invoice_id, lifecycle)
    assert lifecycle.calls == ["suspend", "unsuspend"]


def test_payment_holding_the_billing_lock_when_the_provider_call_ends(sessions, monkeypatch):
    from app.services.service_lifecycle import ServiceLifecycle

    billing_id, service_id, invoice_id = _seed(sessions)
    locked, release = threading.Event(), threading.Event()
    payer = threading.Thread(target=_pay, args=(sessions, invoice_id), kwargs={"locked": locked, "release": release})

    def start_payment():
        payer.start()
        assert locked.wait(10)
        # The payment holds the billing lock; it commits while the job waits for it.
        threading.Timer(0.5, release.set).start()

    lifecycle = FakeLifecycle(start_payment)
    monkeypatch.setattr(ServiceLifecycle, "unsuspend", _through(lifecycle))
    results: list = []
    _run_once(sessions, billing_id, lifecycle, results)
    payer.join(30)
    assert results == ["done"], results
    _assert_lifted(sessions, billing_id, service_id, invoice_id, lifecycle)


def test_payment_arriving_while_the_job_holds_the_billing_lock(sessions, monkeypatch):
    from app.services.billing.jobs import suspension
    from app.services.service_lifecycle import ServiceLifecycle

    billing_id, service_id, invoice_id = _seed(sessions)
    payer = threading.Thread(target=_pay, args=(sessions, invoice_id))
    real_marks = suspension.suspension_marks

    def marks_then_pay(db):
        # Called as the job marks its claim done: the payment starts now and
        # must wait for the job's commit.
        marks = real_marks(db)
        payer.start()
        time.sleep(0.5)
        return marks

    monkeypatch.setattr(suspension, "suspension_marks", marks_then_pay)
    lifecycle = FakeLifecycle(lambda: None)
    # The payment's own unsuspension after commit goes through ServiceLifecycle.
    monkeypatch.setattr(ServiceLifecycle, "unsuspend", _through(lifecycle))
    results: list = []
    _run_once(sessions, billing_id, lifecycle, results)
    payer.join(30)
    assert results == ["done"], results
    with sessions() as db:
        from app.models.commerce_email import EmailMessage

        # The job saw the invoice unpaid and sent its email before the payment.
        assert db.query(EmailMessage).filter_by(event="service_suspended_nonpayment").count() == 1
        db.query(EmailMessage).delete()
        db.commit()
    _assert_lifted(sessions, billing_id, service_id, invoice_id, lifecycle)
