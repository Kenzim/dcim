"""A reseller payment racing the recurring pass's non-payment suspension on MariaDB (optional, design 15.5).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards.

The recurring pass suspends a reseller service inside the service billing's
locked transaction, and a reseller cycle invoice paid afterwards is completed
(and the service unsuspended) by the next pass's paid-cycle recovery. A
payment or credit application landing before the pass, while the pass holds
the billing lock during the provider call, or after the pass, therefore ends
with the service active, the billing active and the cycle paid once the next
pass has run: never a paid invoice with the service still down. A credit
top-up alone is enough: the next pass applies credit that covers the whole
balance to the suspended cycle's invoice (credit only, never a card).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from tests.services.test_billing_p1c_credit_mariadb import _drop_all

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")

DUE = datetime(2027, 6, 1, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["default_isolation", "read_committed"])
def sessions(request, monkeypatch):
    import app.models  # noqa: F401
    import app.core.database as database_module
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
    monkeypatch.setattr(plugin_events, "emit", lambda *args, **kwargs: None)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", factory)
    yield factory
    _drop_all(engine)
    engine.dispose()


class RacingLifecycle:
    """Suspends without a provider; ``on_suspend`` runs where the power-off would."""

    def __init__(self):
        self.on_suspend = None
        self.calls: list[str] = []

    async def suspend(self, db, service, *, reason):
        from app.models.service import ServiceStatus

        if service.status in {ServiceStatus.SUSPENDED, ServiceStatus.TERMINATED}:
            return False
        self.calls.append("suspend")
        if self.on_suspend is not None:
            hook, self.on_suspend = self.on_suspend, None
            hook()
        service.status = ServiceStatus.SUSPENDED
        db.flush()
        return True

    async def unsuspend(self, db, service, *, reason):
        from app.models.service import ServiceStatus

        if service.status != ServiceStatus.SUSPENDED:
            return False
        self.calls.append("unsuspend")
        service.status = ServiceStatus.ACTIVE
        db.flush()
        return True


def _worker(lifecycle, now):
    from app.services.recurring_billing_service import RecurringBillingService
    from tests.services.test_recurring_billing_service import FakeGateway, MutableClock, _config

    clock = MutableClock(now)
    worker = RecurringBillingService(config=_config(), gateway=FakeGateway(["fail"] * 4), lifecycle=lifecycle, clock=clock)
    return worker, clock


def _pass(sessions, worker) -> dict:
    with sessions() as db:
        return asyncio.run(worker.process_batch(db, owner="maria-rsl"))


def _seed_in_grace(sessions):
    """A reseller billing whose four charge attempts failed; the pass at DUE + 7 days suspends it."""
    from app.models.reseller import BillingCycle
    from tests.services.test_recurring_billing_service import _billing, _reseller

    with sessions() as db:
        billing = _billing(db, _reseller(db, "maria-rsl"), "maria-rsl", DUE)
        db.commit()
        ids = billing.id, billing.service_id, billing.reseller_id
    lifecycle = RacingLifecycle()
    worker, clock = _worker(lifecycle, DUE)
    for day in (0, 1, 3, 5):
        clock.current = DUE + timedelta(days=day)
        _pass(sessions, worker)
    with sessions() as db:
        invoice_id = db.query(BillingCycle).filter_by(service_billing_id=ids[0]).one().invoice_id
    clock.current = DUE + timedelta(days=7)
    return (*ids, invoice_id), worker, clock, lifecycle


def _pay(sessions, invoice_id: int) -> None:
    """Settle the invoice in full through a gateway payment (the billing lock first, as every payment path)."""
    from app.models.reseller import Payment, PaymentStatus
    from app.services.billing import settlement
    from app.services.billing_locks import BillingLocks

    with sessions() as db:
        invoice = BillingLocks.for_invoice(db, invoice_id, reseller=True, billing_account=True)
        payment = Payment(
            invoice_id=invoice.id, gateway="late", status=PaymentStatus.SUCCEEDED, amount_cents=invoice.amount_cents,
            refunded_cents=0, currency=invoice.currency, external_ref=f"late:{invoice.id}", payment_metadata={},
        )
        db.add(payment)
        db.flush()
        settlement.settle_invoice(db, invoice.id, payment=payment)
        settlement.commit_and_fulfil(db)


def _apply_credit(sessions, invoice_id: int) -> None:
    """Top up the reseller's credit and apply it to the invoice (the reseller panel's apply-credit)."""
    from app.models.reseller import Invoice
    from app.services.credit_ledger_service import CreditLedgerService

    with sessions() as db:
        invoice = db.get(Invoice, invoice_id)
        CreditLedgerService.credit(
            db, int(invoice.reseller_id), int(invoice.amount_cents), description="top-up",
            idempotency_key=f"topup:{invoice_id}",
        )
        db.commit()
    with sessions() as db:
        CreditLedgerService.apply_to_invoice(db, db.get(Invoice, invoice_id), idempotency_key=f"apply:{invoice_id}")
        db.commit()


def _state(sessions, ids):
    from app.models.reseller import BillingCycle, Invoice, ServiceBilling
    from app.models.service import Service

    billing_id, service_id, _, invoice_id = ids
    with sessions() as db:
        return (
            db.get(Invoice, invoice_id).status,
            db.get(Service, service_id).status,
            db.get(ServiceBilling, billing_id).status,
            db.query(BillingCycle).filter_by(service_billing_id=billing_id).one().state,
        )


def _assert_recovered(sessions, ids, lifecycle, *, suspended: bool) -> None:
    from app.models.reseller import BillingCycleState, InvoiceStatus, ServiceBillingStatus
    from app.models.service import ServiceStatus

    assert _state(sessions, ids) == (
        InvoiceStatus.PAID, ServiceStatus.ACTIVE, ServiceBillingStatus.ACTIVE, BillingCycleState.PAID,
    )
    assert lifecycle.calls == (["suspend", "unsuspend"] if suspended else [])


def _in_background(target, *args):
    errors: list = []

    def run():
        try:
            target(*args)
        except Exception as exc:  # noqa: BLE001 - reported through errors
            errors.append(repr(exc))

    thread = threading.Thread(target=run)
    return thread, errors


@pytest.mark.parametrize("payer", [_pay, _apply_credit])
def test_paid_before_the_pass_is_never_suspended(sessions, payer):
    ids, worker, clock, lifecycle = _seed_in_grace(sessions)
    payer(sessions, ids[3])
    _pass(sessions, worker)
    _assert_recovered(sessions, ids, lifecycle, suspended=False)


@pytest.mark.parametrize("payer", [_pay, _apply_credit])
def test_paid_while_the_pass_holds_the_billing_during_the_provider_call(sessions, payer):
    from app.models.reseller import InvoiceStatus
    from app.models.service import ServiceStatus

    ids, worker, clock, lifecycle = _seed_in_grace(sessions)
    thread, errors = _in_background(payer, sessions, ids[3])

    def start_payment():
        thread.start()
        # The payment waits for the billing lock the pass holds.
        time.sleep(0.5)

    lifecycle.on_suspend = start_payment
    _pass(sessions, worker)
    thread.join(30)
    assert not thread.is_alive() and errors == [], errors
    # Paid after the suspension committed: down until the next pass.
    assert _state(sessions, ids)[:2] == (InvoiceStatus.PAID, ServiceStatus.SUSPENDED)
    clock.current += timedelta(minutes=1)
    _pass(sessions, worker)
    _assert_recovered(sessions, ids, lifecycle, suspended=True)


def test_payment_row_committed_during_the_provider_call_without_the_lock(sessions):
    """A gateway success recorded before its settlement (no lock), settled after the pass."""
    from app.models.reseller import Invoice, Payment, PaymentStatus
    from app.services.invoice_service import InvoiceService

    ids, worker, clock, lifecycle = _seed_in_grace(sessions)
    invoice_id = ids[3]

    def record_payment():
        with sessions() as db:
            invoice = db.get(Invoice, invoice_id)
            db.add(Payment(
                invoice_id=invoice_id, gateway="late", status=PaymentStatus.SUCCEEDED, amount_cents=invoice.amount_cents,
                refunded_cents=0, currency=invoice.currency, external_ref=f"row:{invoice_id}", payment_metadata={},
            ))
            db.commit()

    lifecycle.on_suspend = record_payment
    _pass(sessions, worker)
    with sessions() as db:
        InvoiceService.mark_paid_if_fully_allocated(db, invoice_id)
        db.commit()
    clock.current += timedelta(minutes=1)
    _pass(sessions, worker)
    _assert_recovered(sessions, ids, lifecycle, suspended=True)


@pytest.mark.parametrize("payer", [_pay, _apply_credit])
def test_paid_after_the_pass_is_recovered_by_the_next_pass(sessions, payer):
    ids, worker, clock, lifecycle = _seed_in_grace(sessions)
    _pass(sessions, worker)
    payer(sessions, ids[3])
    clock.current += timedelta(minutes=1)
    _pass(sessions, worker)
    _assert_recovered(sessions, ids, lifecycle, suspended=True)


def test_a_credit_top_up_alone_restores_the_service(sessions):
    """The next pass pays the suspended cycle from new credit (credit only) and recovery unsuspends it."""
    from app.models.reseller import Invoice
    from app.services.credit_ledger_service import CreditLedgerService

    ids, worker, clock, lifecycle = _seed_in_grace(sessions)
    _pass(sessions, worker)
    with sessions() as db:
        invoice = db.get(Invoice, ids[3])
        CreditLedgerService.credit(db, int(invoice.reseller_id), int(invoice.amount_cents) * 2, description="top-up",
                                   idempotency_key="topup-only")
        db.commit()
    clock.current = DUE + timedelta(days=7, minutes=1)
    _pass(sessions, worker)
    _assert_recovered(sessions, ids, lifecycle, suspended=True)
    # The four failed charges of the seed are the only gateway calls.
    assert len(worker.gateway.calls) == 4


def test_credit_short_of_the_balance_applies_nothing(sessions):
    from app.models.reseller import Invoice, InvoiceStatus, Reseller
    from app.models.service import ServiceStatus
    from app.services.credit_ledger_service import CreditLedgerService

    ids, worker, clock, lifecycle = _seed_in_grace(sessions)
    _pass(sessions, worker)
    with sessions() as db:
        invoice = db.get(Invoice, ids[3])
        CreditLedgerService.credit(db, int(invoice.reseller_id), int(invoice.amount_cents) - 1, description="top-up",
                                   idempotency_key="topup-short")
        db.commit()
    for minutes in (1, 60):
        clock.current = DUE + timedelta(days=7, minutes=minutes)
        _pass(sessions, worker)
    assert _state(sessions, ids)[:2] == (InvoiceStatus.OVERDUE, ServiceStatus.SUSPENDED)
    with sessions() as db:
        assert CreditLedgerService.available_cents(db, db.get(Reseller, ids[2])) == int(
            db.get(Invoice, ids[3]).amount_cents
        ) - 1
