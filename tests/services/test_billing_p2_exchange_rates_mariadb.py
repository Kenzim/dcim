"""Exchange rates on MariaDB (optional; design 4.10).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards.

A publish that freezes a sterling VAT total share-locks the stored rate
(``exchange_rates.convert_at_publish``). The admin delete locks the rate for
update first, so it waits for that publish to commit and then sees its
invoice and refuses. A publish is not held up by another holding the rate. A rate stored twice at once (the job and an administrator) leaves one
row: the unique key refuses the second.

A proforma paid (through the real payment path: a payment settled by
``InvoiceService.recompute``, which takes the series counter's lock before
the rate's) in a month whose rate is missing leaves its sterling VAT
pending; a rate stored meanwhile waits for the payment (the payment's share
lock on the missing row) and then fills the invoice, and a payment during a
store waits for it and takes the new rate. A store that comes between the
payment's rate read and its write waits for the payment's gap lock under
REPEATABLE READ and fills the invoice; under READ COMMITTED it misses it and
the daily sweep fills it.

A credit note counts the earlier notes' VAT with a locking read, so a note
committed after its transaction's snapshot still counts, and the notes'
sterling adds up to the invoice's. The invoice guard reads the stored row
with a locking read before it allows a sterling fill, so a session copy
loaded before another transaction filled the row cannot fill it again.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
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
def engine(request):
    import app.models  # noqa: F401
    from app.core.database import Base, configure_engine

    database = sa.engine.make_url(URL).database or ""
    if not database.startswith("rackflow_test"):
        pytest.fail("RACKFLOW_TEST_MARIADB_URL must name a database starting with rackflow_test")
    options = {"pool_pre_ping": True, "pool_size": 6, "max_overflow": 0}
    if request.param == "read_committed":
        options["isolation_level"] = "READ COMMITTED"
    built = configure_engine(sa.create_engine(URL, **options))
    _drop_all(built)
    Base.metadata.create_all(built)
    yield built
    _drop_all(built)
    built.dispose()


@pytest.fixture
def sessions(engine, monkeypatch):
    from tests.services.tax_helpers import stub_legal_notes

    stub_legal_notes(monkeypatch)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _period() -> str:
    from app.services.tax.exchange_rates import period_of

    return period_of(datetime.now(timezone.utc))


def _seed(sessions, invoices: int = 1, *, rate: bool = True) -> tuple[int, list[int]]:
    """Tax v2 with a GB registration, this month's USD rate, and draft USD invoices; ``(rate_id, invoice_ids)``."""
    from app.models.commerce_invoice_ext import InvoiceLineKind
    from app.models.reseller import InvoicePurpose
    from app.services.invoice_service import InvoiceService, NewLine
    from app.services.tax import exchange_rates
    from tests.services.tax_helpers import (
        add_registration,
        add_rule,
        enable_tax,
        invoice_v2,
        retail_account,
        seed_classes,
        set_engine,
    )

    with sessions() as db:
        seed_classes(db)
        invoice_v2(db)
        set_engine(db, "tax", "v2")
        enable_tax(db, "GB")
        add_registration(db, "GB")
        add_rule(db, name="UK VAT 20%", jurisdiction="GB", source="pack", legal_note_key=None)
        rate_id = None
        if rate:
            exchange_rates.store_month(db, _period(), [("USD", "1.3396")], fetched_at=datetime.now(timezone.utc))
            rate_id = exchange_rates.stored_rate(db, "USD", _period()).id
        ids = []
        for _ in range(invoices):
            _user, account = retail_account(db, country="GB")
            invoice = InvoiceService.create(
                db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1000,
                currency="USD", publish=False, lines=[NewLine("Server", 1000, kind=InvoiceLineKind.PRODUCT)],
            )
            ids.append(invoice.id)
        db.commit()
        return rate_id, ids


def _publish(db, invoice_id: int):
    from app.models.reseller import Invoice
    from app.services.invoice_service import InvoiceService

    invoice = db.get(Invoice, invoice_id)
    InvoiceService.publish(db, invoice)
    return invoice


def _delete(sessions, rate_id: int, outcome: dict, *, read_first: bool = False) -> threading.Thread:
    """The admin delete in a thread; ``read_first`` makes a plain read before it.

    Under REPEATABLE READ that read fixes the transaction's snapshot before
    the publish commits, so a plain in-use check after it would not see the
    publish's invoice.
    """
    from app.api.billing_tax_exchange_rates_admin import delete_rate
    from app.models.reseller import Invoice

    def run():
        with sessions() as db:
            db.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 20"))
            if read_first:
                db.execute(sa.select(sa.func.count(Invoice.id))).scalar()
                outcome["read"] = True
            try:
                delete_rate(rate_id, {"user_id": None}, db)
                outcome["result"] = "deleted"
            except HTTPException as exc:
                outcome["result"] = exc.detail
                db.rollback()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


@pytest.mark.parametrize("read_first", [False, True], ids=["no_earlier_read", "earlier_plain_read"])
def test_a_delete_during_a_publish_waits_and_then_refuses(sessions, read_first):
    from app.models.tax import TaxExchangeRate

    rate_id, (invoice_id,) = _seed(sessions)
    publisher = sessions()
    outcome: dict = {}
    try:
        invoice = _publish(publisher, invoice_id)
        assert (invoice.fx_period, invoice.tax_total_gbp_cents, invoice.fx_rate_id) == (_period(), 149, rate_id)
        thread = _delete(sessions, rate_id, outcome, read_first=read_first)
        time.sleep(1.5)
        assert thread.is_alive(), outcome
        publisher.commit()
        thread.join(timeout=20)
        assert not thread.is_alive()
    finally:
        publisher.close()
    assert outcome["result"]["code"] == "tax_exchange_rate_in_use"
    with sessions() as db:
        assert db.get(TaxExchangeRate, rate_id) is not None


def test_a_delete_committed_first_makes_the_publish_refuse(sessions):
    from app.models.reseller import Invoice
    from app.services.tax.exchange_rates import ExchangeRateMissing

    rate_id, (invoice_id,) = _seed(sessions)
    outcome: dict = {}
    _delete(sessions, rate_id, outcome).join(timeout=20)
    assert outcome["result"] == "deleted"
    with sessions() as db:
        with pytest.raises(ExchangeRateMissing):
            _publish(db, invoice_id)
        db.rollback()
        assert db.get(Invoice, invoice_id).status.value == "draft"


def test_a_publish_is_not_held_up_by_another_holding_the_rate(sessions):
    """Another publish's lock on the rate is shared, so this publish takes it at once.

    (Two whole publishes also share the invoice number series, which serialises
    them for its own reasons, so the other publish is stood in for by its
    shared read of the rate.)
    """
    from app.models.tax import TaxExchangeRate

    rate_id, (invoice_id,) = _seed(sessions)
    holder, publisher = sessions(), sessions()
    try:
        holder.execute(sa.select(TaxExchangeRate.id).where(TaxExchangeRate.id == rate_id).with_for_update(read=True)).all()
        publisher.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 2"))
        assert _publish(publisher, invoice_id).tax_total_gbp_cents == 149
        publisher.commit()
    finally:
        holder.rollback()
        holder.close()
        publisher.close()


def test_the_same_rate_stored_twice_at_once_leaves_one_row(sessions):
    from app.models.tax import TaxExchangeRate
    from app.services.tax import exchange_rates

    _seed(sessions, invoices=0, rate=False)
    first, second = sessions(), sessions()
    try:
        second.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 20"))
        exchange_rates.store_month(first, "2030-01", [("USD", "1.3")])
        outcome: dict = {}

        def store():
            try:
                exchange_rates.store_month(second, "2030-01", [("USD", "1.4")])
                second.commit()
                outcome["result"] = "stored"
            except sa.exc.IntegrityError:
                second.rollback()
                outcome["result"] = "refused"

        thread = threading.Thread(target=store)
        thread.start()
        time.sleep(1.5)
        assert thread.is_alive(), "the second insert did not wait for the first"
        first.commit()
        thread.join(timeout=20)
    finally:
        first.close()
        second.close()
    assert outcome["result"] == "refused"
    with sessions() as db:
        rows = db.scalars(sa.select(TaxExchangeRate).where(TaxExchangeRate.period == "2030-01")).all()
        assert [(r.currency, str(r.units_per_gbp)) for r in rows] == [("USD", "1.300000")]


def _published(sessions) -> int:
    rate_id, (invoice_id,) = _seed(sessions)
    with sessions() as db:
        _publish(db, invoice_id)
        db.commit()
    return invoice_id


def _proforma(sessions, monkeypatch) -> int:
    """A proforma published now with this month's rate; afterwards the clock reads ``LATER`` (March 2031)."""
    import app.services.invoice_service as invoice_service
    from app.models.billing_series import DocumentType
    from app.models.commerce_invoice_ext import InvoiceLineKind
    from app.models.reseller import InvoiceDocumentType, InvoicePurpose
    from app.services.invoice_service import InvoiceService, NewLine
    from app.services.settings_store import set_setting
    from tests.services.tax_helpers import retail_account
    from tests.services.test_billing_p1i_invoice_engine import _series

    _seed(sessions, invoices=0)
    with sessions() as db:
        set_setting(db, "billing_proforma_enabled", True)
        _series(db, DocumentType.PROFORMA, "PF")
        _user, account = retail_account(db, country="GB")
        invoice = InvoiceService.create(
            db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1000,
            currency="USD", lines=[NewLine("Server", 1000, kind=InvoiceLineKind.PRODUCT)],
        )
        assert invoice.document_type == InvoiceDocumentType.PROFORMA and invoice.fx_rate is not None
        invoice_id = int(invoice.id)
        db.commit()
    monkeypatch.setattr(invoice_service, "_utcnow", lambda: LATER)
    return invoice_id


def _pay(db, invoice_id: int):
    """The real payment path: a succeeded payment settled by ``recompute``, which converts the proforma."""
    from app.models.reseller import Invoice, InvoiceDocumentType, Payment, PaymentStatus
    from app.services.invoice_service import InvoiceService

    invoice = db.get(Invoice, invoice_id)
    payment = Payment(invoice_id=invoice_id, gateway="manual", status=PaymentStatus.SUCCEEDED,
                      amount_cents=int(invoice.amount_cents), refunded_cents=0, currency="USD", payment_metadata={})
    db.add(payment)
    db.flush()
    InvoiceService.recompute(db, invoice, cause="payment", payment=payment)
    assert invoice.document_type == InvoiceDocumentType.INVOICE
    return invoice


def _isolation(db) -> str:
    return str(db.execute(sa.text("SELECT @@tx_isolation")).scalar())


LATER = datetime(2031, 3, 10, 12, tzinfo=timezone.utc)


def test_a_rate_stored_during_a_pending_payment_waits_and_then_fills_it(sessions, monkeypatch):
    """The payment's share lock on the missing rate holds the store back until the payment commits."""
    from app.models.reseller import Invoice
    from app.services.tax import exchange_rates

    invoice_id = _proforma(sessions, monkeypatch)
    payer, storer = sessions(), sessions()
    outcome: dict = {}
    try:
        invoice = _pay(payer, invoice_id)
        assert (invoice.fx_period, invoice.fx_rate) == ("2031-03", None)
        storer.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 20"))

        def store():
            outcome["filled"] = exchange_rates.store_month(storer, "2031-03", [("USD", "1.3158")]).filled
            storer.commit()

        thread = threading.Thread(target=store)
        thread.start()
        time.sleep(1.5)
        assert thread.is_alive(), "the store did not wait for the payment"
        payer.commit()
        thread.join(timeout=20)
        assert not thread.is_alive()
    finally:
        payer.close()
        storer.close()
    assert outcome["filled"] == (invoice_id,)
    with sessions() as db:
        invoice = db.get(Invoice, invoice_id)
        rate = exchange_rates.stored_rate(db, "USD", "2031-03")
        assert (invoice.fx_period, invoice.tax_total_gbp_cents, invoice.fx_rate_id) == ("2031-03", 152, rate.id)


def test_a_store_between_the_payments_rate_read_and_its_write(sessions, monkeypatch):
    """The payment is paused just after it reads "no rate", before it writes pending; then a rate is stored.

    Under REPEATABLE READ (production) the payment's share lock on the
    missing row is a gap lock: the store's insert waits for the payment to
    commit, then its fill sees the pending invoice and fills it. Without that
    lock the store would commit first and miss it. READ COMMITTED takes no
    gap lock, so there the store misses the invoice and the daily sweep
    fills it (design 4.10).
    """
    from app.models.commerce_audit import UserAuditEvent
    from app.models.reseller import Invoice
    from app.services.tax import exchange_rates, sterling_conversion

    invoice_id = _proforma(sessions, monkeypatch)
    read, release = threading.Event(), threading.Event()
    original = exchange_rates.stored_rate

    def paused(db, currency, period, *args, **kwargs):
        row = original(db, currency, period, *args, **kwargs)
        if threading.current_thread().name == "payer" and period == "2031-03":
            read.set()
            assert release.wait(30)
        return row

    monkeypatch.setattr(exchange_rates, "stored_rate", paused)
    outcome: dict = {}

    def pay():
        with sessions() as db:
            outcome["payer_isolation"] = _isolation(db)
            invoice = _pay(db, invoice_id)
            outcome["paid"] = (invoice.fx_period, invoice.fx_rate)
            db.commit()

    def store():
        with sessions() as db:
            db.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 20"))
            outcome["filled"] = exchange_rates.store_month(db, "2031-03", [("USD", "1.3158")]).filled
            db.commit()

    payer = threading.Thread(target=pay, name="payer")
    payer.start()
    assert read.wait(30), "the payment never read the rate"
    storer = threading.Thread(target=store, name="storer")
    storer.start()
    time.sleep(1.5)
    outcome["store_waited"] = storer.is_alive()
    release.set()
    payer.join(timeout=30)
    storer.join(timeout=30)
    assert not payer.is_alive() and not storer.is_alive()
    assert outcome["paid"] == ("2031-03", None)
    with sessions() as db:
        filled_audits = db.scalar(
            sa.select(sa.func.count(UserAuditEvent.id)).where(UserAuditEvent.action == "billing.tax.sterling_filled")
        )
        invoice = db.get(Invoice, invoice_id)
        if outcome["payer_isolation"] == "REPEATABLE-READ":
            assert outcome["store_waited"], "the store did not wait for the payment's gap lock"
            assert outcome["filled"] == (invoice_id,)
            assert (invoice.tax_total_gbp_cents, filled_audits) == (152, 1)
        else:
            assert outcome["payer_isolation"] == "READ-COMMITTED"
            assert outcome["filled"] == ()
            assert sterling_conversion.is_pending(invoice)
            assert sterling_conversion.sweep(db, "2031-03", "USD") == [invoice_id]
            db.commit()
            db.refresh(invoice)
            assert invoice.tax_total_gbp_cents == 152


def test_a_payment_during_a_store_waits_for_it_and_takes_the_rate(sessions, monkeypatch):
    """The store's new row holds the payment's share lock back; the store's fill is not held by the payment."""
    from app.models.reseller import Invoice
    from app.services.tax import exchange_rates

    invoice_id = _proforma(sessions, monkeypatch)
    payer, storer = sessions(), sessions()
    stored = threading.Event()

    def store():
        exchange_rates.store_month(storer, "2031-03", [("USD", "1.3158")])
        stored.set()
        time.sleep(1.5)
        storer.commit()

    try:
        payer.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 20"))
        payer.get(Invoice, invoice_id, with_for_update=True)
        thread = threading.Thread(target=store)
        thread.start()
        assert stored.wait(timeout=20), "the store was held up by the locked invoice"
        started = time.monotonic()
        invoice = _pay(payer, invoice_id)
        assert time.monotonic() - started > 0.5, "the payment did not wait for the store"
        rate_id = invoice.fx_rate_id
        assert (invoice.fx_period, invoice.tax_total_gbp_cents) == ("2031-03", 152)
        payer.commit()
        thread.join(timeout=20)
    finally:
        payer.close()
        storer.close()
    with sessions() as db:
        assert exchange_rates.stored_rate(db, "USD", "2031-03").id == rate_id


def _credit(db, invoice_id: int, amount: int, key: str):
    from app.models.invoice_adjustment import AdjustmentOutcome, AdjustmentReason
    from app.models.reseller import Invoice
    from app.services.invoice_adjustment_service import InvoiceAdjustmentService

    return InvoiceAdjustmentService.issue_credit_note(
        db, db.get(Invoice, invoice_id), amount_cents=amount, reason=AdjustmentReason.GOODWILL,
        outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None, idempotency_key=key,
    )


def test_a_credit_note_counts_a_note_committed_after_its_snapshot(sessions):
    """A reads first, B credits half and commits, then A credits the rest.

    USD 1.00 of VAT alone is GBP 0.75 (0.7465), so two halves each counted
    from nothing would make GBP 1.50 against the invoice's 1.49.
    """
    from app.models.billing_series import DocumentType
    from app.models.invoice_adjustment import InvoiceAdjustment
    from app.models.reseller import Invoice
    from tests.services.test_billing_p1i_invoice_engine import _series

    invoice_id = _published(sessions)
    with sessions() as db:
        _series(db, DocumentType.CREDIT_NOTE, "CN")
        db.commit()
        invoice = db.get(Invoice, invoice_id)
        total, sterling = int(invoice.amount_cents), invoice.tax_total_gbp_cents
    assert (invoice.tax_cents, sterling) == (200, 149)
    late, early = sessions(), sessions()
    try:
        assert late.execute(sa.select(sa.func.count(InvoiceAdjustment.id))).scalar() == 0
        first = _credit(early, invoice_id, total // 2, "fx-race-early")
        early.commit()
        second = _credit(late, invoice_id, total - total // 2, "fx-race-late")
        late.commit()
    finally:
        early.close()
        late.close()
    assert (first.tax_cents, second.tax_cents) == (100, 100)
    assert (first.tax_total_gbp_cents, second.tax_total_gbp_cents) == (75, 74)
    assert first.tax_total_gbp_cents + second.tax_total_gbp_cents == sterling


def test_the_guard_refuses_a_second_fill_from_a_copy_loaded_before_the_first(sessions, monkeypatch):
    """A loads the pending invoice, B fills it and commits, then A's own fill, with the same values, is refused.

    The rate is stored before A's snapshot without filling (as a store that
    missed the invoice leaves it), so A's fill names the right row, value,
    source and figure, and only the guard's locking reread can refuse it.
    """
    from app.models.reseller import Invoice
    from app.models.tax import TaxExchangeRate
    from app.services import invoice_guard
    from app.services.invoice_guard import FrozenInvoiceError
    from app.services.tax import exchange_rates, sterling_conversion

    invoice_id = _proforma(sessions, monkeypatch)
    with sessions() as db:
        _pay(db, invoice_id)
        db.commit()
        db.add(TaxExchangeRate(
            source="hmrc_monthly", base="GBP", currency="USD", period="2031-03", units_per_gbp=Decimal("1.3158"),
            period_start=datetime(2031, 3, 1).date(), period_end=datetime(2031, 3, 31).date(), fetched_at=LATER,
        ))
        db.commit()
        filled_id = exchange_rates.stored_rate(db, "USD", "2031-03").id
    stale, storer = sessions(), sessions()
    previous = invoice_guard.set_mode_override("enforce")
    try:
        invoice = stale.get(Invoice, invoice_id)
        assert (invoice.fx_period, invoice.fx_rate) == ("2031-03", None)
        assert sterling_conversion.sweep(storer, "2031-03", "USD") == [invoice_id]
        storer.commit()
        invoice.fx_rate = Decimal("1.3158")
        invoice.fx_source = "hmrc_monthly"
        invoice.tax_total_gbp_cents = 152
        invoice.fx_rate_id = filled_id
        with pytest.raises(FrozenInvoiceError):
            stale.flush()
        stale.rollback()
    finally:
        invoice_guard.set_mode_override(previous)
        stale.close()
        storer.close()
    with sessions() as db:
        invoice = db.get(Invoice, invoice_id)
        assert (invoice.fx_rate, invoice.fx_source, invoice.tax_total_gbp_cents, invoice.fx_rate_id) == (
            Decimal("1.3158"), "hmrc_monthly", 152, filled_id
        )
