"""Billing job claims and leases under concurrency on MariaDB (optional, design 15.5).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards (see ``test_billing_p1c_credit_mariadb.py``).
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

T0 = datetime(2027, 5, 10, 9, 30, tzinfo=timezone.utc)
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

    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with make() as db:
        for name in BILLING_JOB_NAMES:
            db.add(BillingJobState(job=name))
        db.commit()
    return make


class _Job:
    """Subjects are ``(id, trigger)``; the effect is a ``system_settings`` row."""

    name = NAME
    setting_keys = ("billing_invoice_lead_days",)

    def __init__(self, subjects, on_execute=None):
        self.subjects = dict(subjects)
        self.on_execute = on_execute

    def candidates(self, db, run):
        return sorted(self.subjects)

    def plan(self, db, run, subject_id):
        from app.services.billing.jobs.base import PlannedAction

        at = self.subjects[subject_id]
        return PlannedAction(f"s:{subject_id}", "touch", at, {"id": subject_id}) if run.reached(at) else None

    def execute(self, db, run, action):
        from app.models.commerce_account import SystemSetting

        if not run.claim(db, action.subject_key):
            return "already_done"
        if self.on_execute:
            self.on_execute(db, run, action)
        db.add(SystemSetting(key=f"fx-{action.detail['id']}", value=1))
        db.flush()
        return "done"


@pytest.fixture
def job(monkeypatch):
    from app.services.billing.jobs import registry

    fake = _Job({})
    monkeypatch.setattr(registry, "JOBS", {NAME: fake})
    return fake


def _go_live(factory, at):
    from app.services.billing.jobs import runner

    for to, when in (("dry_run", at - timedelta(days=1)), ("live", at)):
        with factory() as db:
            runner.change_mode(db, NAME, to, now=when, actor_user_id=None)
            db.commit()
        if to == "dry_run":
            assert runner.run_job(factory, NAME, dry_run=True, trigger="schedule", now=when).status == "complete"


def _effects(factory):
    from app.models.commerce_account import SystemSetting

    with factory() as db:
        return sorted(row.key for row in db.query(SystemSetting).filter(SystemSetting.key.like("fx-%")))


def test_two_sessions_claiming_one_subject_give_one_effect(factory):
    """The second claim waits for the first transaction, then fails on the unique key."""
    from app.models.billing_jobs import BillingJobAction
    from app.models.commerce_account import SystemSetting
    from app.services.billing.jobs.base import JobRun

    first_run = JobRun(run_id=None, job=NAME, dry_run=False, now=T0, watermark=T0)
    second_run = JobRun(run_id=None, job=NAME, dry_run=False, now=T0, watermark=T0)
    results: dict[str, bool] = {}
    with factory() as first, factory() as second:
        assert first_run.claim(first, "billing:1:cycle:x")
        first.add(SystemSetting(key="fx-1", value=1))
        first.flush()

        def claim_second():
            # A plain read first fixes the second session's snapshot.
            second.execute(sa.select(BillingJobAction.id)).all()
            results["second"] = second_run.claim(second, "billing:1:cycle:x")
            if results["second"]:
                second.add(SystemSetting(key="fx-2", value=1))
                second.flush()
            second.commit()

        thread = threading.Thread(target=claim_second)
        thread.start()
        thread.join(1.0)
        assert thread.is_alive(), "the second claim must wait for the first transaction"
        first.commit()
        thread.join(30)
        assert not thread.is_alive()
    assert results == {"second": False}
    assert _effects(factory) == ["fx-1"]


def test_a_rolled_back_claim_lets_the_waiting_claim_through(factory):
    from app.services.billing.jobs.base import JobRun

    run = JobRun(run_id=None, job=NAME, dry_run=False, now=T0, watermark=T0)
    results: dict[str, bool] = {}
    with factory() as first, factory() as second:
        assert run.claim(first, "billing:2:cycle:x")

        def claim_second():
            results["second"] = JobRun(run_id=None, job=NAME, dry_run=False, now=T0, watermark=T0).claim(
                second, "billing:2:cycle:x"
            )
            second.commit()

        thread = threading.Thread(target=claim_second)
        thread.start()
        thread.join(1.0)
        assert thread.is_alive()
        first.rollback()
        thread.join(30)
    assert results == {"second": True}


def test_two_runs_of_one_job_the_lease_admits_one(factory, job):
    from app.services.billing.jobs import runner

    _go_live(factory, T0)
    entered = threading.Event()
    release = threading.Event()

    def hold(db, run, action):
        entered.set()
        assert release.wait(30)

    job.subjects = {1: T0 + timedelta(minutes=5)}
    job.on_execute = hold
    results = {}
    first = threading.Thread(
        target=lambda: results.setdefault("first", runner.run_job(factory, NAME, dry_run=False, trigger="admin",
                                                                  now=T0 + timedelta(hours=1), owner="a"))
    )
    first.start()
    assert entered.wait(30)
    results["second"] = runner.run_job(factory, NAME, dry_run=False, trigger="admin",
                                       now=T0 + timedelta(hours=1), owner="b")
    release.set()
    first.join(30)
    assert results["second"].status == "locked"
    assert results["first"].status == "complete" and results["first"].counts == {"done": 1}
    assert _effects(factory) == ["fx-1"]


def test_a_run_that_loses_its_lease_stops(factory, job, monkeypatch):
    from app.models.reseller import RecurringBillingLease
    from app.services.billing.jobs import runner

    _go_live(factory, T0)
    job.subjects = {i: T0 + timedelta(minutes=i) for i in range(1, 4)}
    clock = [0.0]
    monkeypatch.setattr(runner, "_clock", lambda: clock[0])
    with factory() as db:
        from app.services.settings_store import get_setting

        lease = int(get_setting(db, "billing_job_lease_seconds"))

    def steal(db, run, action):
        with factory() as other:
            other.query(RecurringBillingLease).filter_by(name=f"billing_job:{NAME}").update({"owner": "thief"})
            other.commit()
        # Half the lease is spent, so the run renews before its next item.
        clock[0] += lease / 2

    job.on_execute = steal
    result = runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=T0 + timedelta(hours=1))
    assert (result.status, result.error) == ("aborted", "lease lost")
    assert _effects(factory) == ["fx-1"]
