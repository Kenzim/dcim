"""A cancellation request racing invoice generation on one billing, on MariaDB (optional, design 15.5).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards.

The generation side is what a generator must do under 10.1: lock the service
billing, skip when ``cancel_at`` is set, else create the cycle invoice and
commit. Whichever transaction gets the billing lock first, either no invoice
exists for the cancelled period or it is cancelled in the request's
transaction, even when the request's session read before the generator
committed.
"""

from __future__ import annotations

import os
import threading
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


@pytest.fixture(params=["v2", "legacy"])
def engine_mode(request):
    return request.param


def _seed(sessions, mode: str) -> tuple[int, int]:
    from datetime import datetime, timezone

    from app.models.commerce_account import SystemSetting
    from app.services.settings_store import set_setting
    from tests.services.p2l_support import engine_v2, make_account, make_billing, make_service

    with sessions() as db:
        engine_v2(db)
        if mode != "v2":
            set_setting(db, "billing_invoice_engine", mode)
            db.get(SystemSetting, "billing_engine_switch:invoice").value = {"mode": mode}
            db.commit()
        account = make_account(db)
        billing = make_billing(
            db, account, make_service(db, account),
            next_charge_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        return billing.id, billing.service_id


def _generate(sessions, billing_id: int, locked: threading.Event, release: threading.Event, out: list) -> None:
    from datetime import datetime, timezone

    from app.services.billing_locks import BillingLocks
    from app.services.recurring_billing_service import RecurringBillingService

    with sessions() as db:
        billing = BillingLocks.service_billing(db, billing_id)
        locked.set()
        release.wait(10)
        if billing.cancel_at is not None:
            out.append("skipped")
            db.commit()
            return
        cycle = RecurringBillingService()._ensure_cycle(db, billing, datetime.now(timezone.utc))
        db.commit()
        out.append(cycle.invoice_id)


def _request(sessions, service_id: int, out: list, *, before=None) -> None:
    from app.models.service import Service
    from app.services.billing.cancellations import CancellationService

    with sessions() as db:
        try:
            db.get(Service, service_id)  # a plain read first, as the routes do
            if before is not None:
                before()
            request = CancellationService.request(
                db, service_id=service_id, type="end_of_period", reason=None, actor_user_id=None,
                source="client", idempotency_key=f"race-{service_id}",
            )
            db.commit()
            out.append(request.id)
        except Exception as exc:  # noqa: BLE001 - reported through out
            db.rollback()
            out.append(repr(exc))


def _cycle_invoices(sessions, billing_id: int) -> list:
    from app.models.reseller import BillingCycle, Invoice

    with sessions() as db:
        rows = db.execute(
            sa.select(Invoice.status, BillingCycle.state)
            .join(BillingCycle, BillingCycle.invoice_id == Invoice.id)
            .where(BillingCycle.service_billing_id == billing_id)
        ).all()
        return [(status.value, state.value) for status, state in rows]


def test_generator_first_the_request_cancels_the_invoice(sessions, engine_mode):
    billing_id, service_id = _seed(sessions, engine_mode)
    locked, release = threading.Event(), threading.Event()
    generated: list = []
    requested: list = []
    generator = threading.Thread(target=_generate, args=(sessions, billing_id, locked, release, generated))
    generator.start()
    assert locked.wait(10)

    def after_snapshot():
        threading.Timer(0.5, release.set).start()

    requester = threading.Thread(target=_request, args=(sessions, service_id, requested), kwargs={"before": after_snapshot})
    requester.start()
    generator.join(30)
    requester.join(30)
    assert isinstance(generated[0], int), generated
    assert isinstance(requested[0], int), requested
    assert _cycle_invoices(sessions, billing_id) == [("void", "cancelled")]


def test_request_first_nothing_is_generated(sessions, engine_mode):
    billing_id, service_id = _seed(sessions, engine_mode)
    requested: list = []
    _request(sessions, service_id, requested)
    assert isinstance(requested[0], int), requested
    locked, release = threading.Event(), threading.Event()
    release.set()
    generated: list = []
    _generate(sessions, billing_id, locked, release, generated)
    assert generated == ["skipped"]
    assert _cycle_invoices(sessions, billing_id) == []
