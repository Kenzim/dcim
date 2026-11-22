"""generate_invoices_ahead racing the recurring worker's at-due path on MariaDB (optional, design 15.5).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards (see ``test_billing_p1c_credit_mariadb.py``).

Either order ends with one cycle, one invoice and one charge: the job
committing after the worker's snapshot (the worker's cycle lookup is a
locking read and reuses it), and the job running while the worker holds the
billing (it waits for the lock, then finds the cycle).
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from tests.services.test_billing_p1c_credit_mariadb import _drop_all

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")

T0 = datetime(2027, 6, 1, 10, 0, tzinfo=timezone.utc)
NAME = "generate_invoices_ahead"


@pytest.fixture(params=["default_isolation", "read_committed"])
def engine(request):
    import app.models  # noqa: F401
    from app.core.database import Base, configure_engine

    database = sa.engine.make_url(URL).database or ""
    if not database.startswith("rackflow_test"):
        pytest.fail("RACKFLOW_TEST_MARIADB_URL must name a database starting with rackflow_test")
    options = {"pool_pre_ping": True, "pool_size": 8, "max_overflow": 0}
    if request.param == "read_committed":
        options["isolation_level"] = "READ COMMITTED"
    built = configure_engine(sa.create_engine(URL, **options))
    _drop_all(built)
    Base.metadata.create_all(built)
    yield built
    _drop_all(built)
    built.dispose()


@pytest.fixture
def factory(engine):
    from app.models.billing_jobs import BILLING_JOB_NAMES, BillingJobState
    from app.services.billing.jobs import runner
    from app.services.settings_store import set_setting
    from tests.services.test_billing_p1i_invoice_engine import _v2

    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with make() as db:
        for name in BILLING_JOB_NAMES:
            db.add(BillingJobState(job=name))
        _v2(db)
        set_setting(db, "billing_invoice_lead_days", 7)
        db.commit()
    for to, when in (("dry_run", T0 - timedelta(days=1)), ("live", T0)):
        with make() as db:
            runner.change_mode(db, NAME, to, now=when, actor_user_id=None)
            db.commit()
        if to == "dry_run":
            runner.run_job(make, NAME, dry_run=True, trigger="schedule", now=when)
    return make


def _seed(factory, suffix, due):
    from tests.services.test_recurring_billing_service import _billing, _reseller

    with factory() as db:
        billing = _billing(db, _reseller(db, suffix), suffix, due)
        db.commit()
        return billing.id, billing.service_id


def _run_job(factory, now):
    from app.services.billing.jobs import runner

    return runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=now)


def _worker(gateway, now):
    from app.services.recurring_billing_service import RecurringBillingService
    from tests.services.test_recurring_billing_service import FakeLifecycle, MutableClock, _config

    return RecurringBillingService(config=_config(), gateway=gateway, lifecycle=FakeLifecycle(),
                                   clock=MutableClock(now))


def _assert_one_of_each(factory, billing_id, service_id, gateway):
    from app.models.reseller import BillingCycle, BillingCycleState, Invoice, InvoiceStatus

    with factory() as db:
        cycles = db.query(BillingCycle).filter_by(service_billing_id=billing_id).all()
        invoices = db.query(Invoice).filter_by(service_id=service_id).all()
        assert [c.state for c in cycles] == [BillingCycleState.PAID]
        assert [i.status for i in invoices] == [InvoiceStatus.PAID]
        assert gateway.calls == [invoices[0].id]


@pytest.mark.asyncio
async def test_job_commits_after_the_worker_snapshot(factory, monkeypatch):
    from app.services.recurring_billing_service import RecurringBillingService
    from tests.services.test_recurring_billing_service import FakeGateway

    due = T0 + timedelta(days=10)
    billing_id, service_id = _seed(factory, "maria-ahead-1", due)
    gateway = FakeGateway(["success"])
    worker = _worker(gateway, due + timedelta(minutes=1))
    original = RecurringBillingService._locked
    job_results = []

    def snapshot_then_generate(statement, db):
        # A plain read in the worker's transaction fixes its snapshot (REPEATABLE
        # READ); the job then commits the cycle and invoice in another session.
        from app.models.reseller import Invoice

        if not job_results:
            db.execute(sa.select(Invoice.id)).all()
            job_results.append(_run_job(factory, due - timedelta(days=6)))
        return original(statement, db)

    monkeypatch.setattr(RecurringBillingService, "_locked", staticmethod(snapshot_then_generate))
    with factory() as db:
        result = await worker.process_batch(db, owner="maria")
    assert job_results[0].counts == {"done": 1}
    assert result["paid"] == 1
    _assert_one_of_each(factory, billing_id, service_id, gateway)


@pytest.mark.asyncio
async def test_job_runs_while_the_worker_holds_the_billing(factory, monkeypatch):
    from app.services.invoice_service import InvoiceService
    from tests.services.test_recurring_billing_service import FakeGateway

    due = T0 + timedelta(days=10)
    billing_id, service_id = _seed(factory, "maria-ahead-2", due)
    job_results = []
    thread = threading.Thread(target=lambda: job_results.append(_run_job(factory, due - timedelta(days=6))))
    original = InvoiceService.create_cycle

    def create_while_job_waits(db, **kwargs):
        # The worker holds the billing lock and is creating the cycle here.
        if not thread.is_alive() and not job_results:
            thread.start()
            thread.join(1.0)
            assert thread.is_alive(), "the job must wait for the worker's billing lock"
        return original(db, **kwargs)

    monkeypatch.setattr(InvoiceService, "create_cycle", staticmethod(create_while_job_waits))
    from app.services.recurring_billing_service import RecurringBillingService

    initial = RecurringBillingService._initial_cycle_outcome

    async def after_cycle_commit(self, db, **kwargs):
        # The worker committed the cycle and has not yet charged or moved
        # next_charge_at: the job takes the billing now, so its due date still
        # matches and it reaches the locking cycle lookup.
        thread.join(30)
        return await initial(self, db, **kwargs)

    monkeypatch.setattr(RecurringBillingService, "_initial_cycle_outcome", after_cycle_commit)
    gateway = FakeGateway(["success"])
    worker = _worker(gateway, due + timedelta(minutes=1))
    with factory() as db:
        result = await worker.process_batch(db, owner="maria")
    thread.join(30)
    assert result["paid"] == 1
    assert job_results[0].counts == {"skipped": 1}
    from app.models.billing_jobs import BillingJobRunItem

    with factory() as db:
        item = db.query(BillingJobRunItem).filter_by(run_id=job_results[0].run_id).one()
        assert item.detail["skip_reason"] == "cycle_exists"
    _assert_one_of_each(factory, billing_id, service_id, gateway)
