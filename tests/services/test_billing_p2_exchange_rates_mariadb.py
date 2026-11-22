"""Exchange rates on MariaDB (optional; design 4.10).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards.

A publish that freezes a sterling VAT total share-locks the stored rate
(``exchange_rates.convert_at_publish``). The admin delete locks the rate for
update first, so it waits for that publish to commit and then sees its
invoice and refuses. A publish is not held up by another holding the rate. A rate stored twice at once (the job and an administrator) leaves one
row: the unique key refuses the second.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

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


def _delete(sessions, rate_id: int, outcome: dict) -> threading.Thread:
    from app.api.billing_tax_exchange_rates_admin import delete_rate

    def run():
        with sessions() as db:
            db.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 20"))
            try:
                delete_rate(rate_id, {"user_id": None}, db)
                outcome["result"] = "deleted"
            except HTTPException as exc:
                outcome["result"] = exc.detail
                db.rollback()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_a_delete_during_a_publish_waits_and_then_refuses(sessions):
    from app.models.tax import TaxExchangeRate

    rate_id, (invoice_id,) = _seed(sessions)
    publisher = sessions()
    outcome: dict = {}
    try:
        invoice = _publish(publisher, invoice_id)
        assert (invoice.fx_period, invoice.tax_total_gbp_cents) == (_period(), 149)
        thread = _delete(sessions, rate_id, outcome)
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
