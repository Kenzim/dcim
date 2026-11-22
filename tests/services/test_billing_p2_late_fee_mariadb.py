"""The late fee job against concurrent runs and payments on MariaDB (optional, design 15.5).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards. The engine is built through
``configure_engine()`` as the application's is.

Two runs on one overdue invoice add at most one debit note; a payment that
commits while a run waits for the invoice lock leaves no fee, even though the
run planned the fee from an older snapshot.
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
    import app.core.database as database

    monkeypatch.setattr(plugin_events, "emit", lambda *args, **kwargs: None)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    # Number counters and the email outbox open their own sessions.
    monkeypatch.setattr(database, "SessionLocal", factory)
    yield factory
    _drop_all(engine)
    engine.dispose()


def _seed(sessions) -> int:
    from app.services.settings_store import set_setting
    from tests.services.p2l_support import NOW, engine_v2, make_account, make_billing, make_cycle_invoice, make_service

    with sessions() as db:
        engine_v2(db)
        for key, value in (
            ("billing_late_fee_type", "percent"),
            ("billing_late_fee_percent_bp", 1000),
            ("billing_late_fee_days", 3),
        ):
            set_setting(db, key, value)
        db.commit()
        account = make_account(db)
        billing = make_billing(db, account, make_service(db, account))
        return make_cycle_invoice(db, billing, due_at=NOW - timedelta(days=5), amount=1000).id


def _notes(sessions, invoice_id: int) -> int:
    from app.models.invoice_adjustment import InvoiceAdjustment

    with sessions() as db:
        return db.query(InvoiceAdjustment).filter_by(invoice_id=invoice_id).count()


def _run_once(sessions, invoice_id: int, results: list, *, before_execute=None) -> None:
    from app.services.billing.jobs.late_fees import LateFeesJob
    from tests.services.p2l_support import make_run

    job, run = LateFeesJob(), make_run("late_fees")
    with sessions() as db:
        try:
            action = job.plan(db, run, invoice_id)
            if before_execute is not None:
                before_execute()
            outcome = job.execute(db, run, action) if action is not None else "none"
            db.commit()
            results.append(outcome)
        except Exception as exc:  # noqa: BLE001 - reported through results
            db.rollback()
            results.append(repr(exc))


def test_two_runs_add_one_debit_note(sessions):
    invoice_id = _seed(sessions)
    results: list = []
    barrier = threading.Barrier(2)
    threads = [
        threading.Thread(target=_run_once, args=(sessions, invoice_id, results), kwargs={"before_execute": barrier.wait})
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(results) == ["already_done", "done"], results
    assert _notes(sessions, invoice_id) == 1


def _hold_and_pay(sessions, invoice_id: int, locked: threading.Event, release: threading.Event) -> None:
    from app.models.reseller import Payment, PaymentStatus
    from app.services.billing import settlement
    from app.services.billing_locks import BillingLocks

    with sessions() as db:
        invoice = BillingLocks.for_invoice(db, invoice_id, reseller=True, billing_account=True)
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


def test_payment_committed_first_leaves_no_fee(sessions):
    from app.models.reseller import Invoice, InvoiceStatus

    invoice_id = _seed(sessions)
    locked, release = threading.Event(), threading.Event()
    payer = threading.Thread(target=_hold_and_pay, args=(sessions, invoice_id, locked, release))
    payer.start()
    assert locked.wait(10)
    results: list = []

    def planned():
        # The run has planned from its snapshot; let the payment commit while
        # the run waits for the invoice lock.
        threading.Timer(0.5, release.set).start()

    runner = threading.Thread(target=_run_once, args=(sessions, invoice_id, results), kwargs={"before_execute": planned})
    runner.start()
    payer.join(30)
    runner.join(30)
    assert results == ["skipped"], results
    assert _notes(sessions, invoice_id) == 0
    with sessions() as db:
        assert db.get(Invoice, invoice_id).status == InvoiceStatus.PAID


def test_fee_committed_first_is_paid_with_the_invoice(sessions):
    invoice_id = _seed(sessions)
    results: list = []
    _run_once(sessions, invoice_id, results)
    assert results == ["done"]
    locked, release = threading.Event(), threading.Event()
    release.set()
    _hold_and_pay(sessions, invoice_id, locked, release)
    time.sleep(0)
    assert _notes(sessions, invoice_id) == 1
