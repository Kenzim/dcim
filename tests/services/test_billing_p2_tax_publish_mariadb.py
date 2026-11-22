"""A tax rule superseded during a publish, on MariaDB (optional; design 15.5).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards.

The publish reads the rules with plain reads and never locks a rule row for
update. Its insert of ``invoice_line_taxes`` (``tax_rule_id`` is a foreign key)
holds InnoDB's shared lock on the rule row until it commits, so a supersede
(lock the old row, close it, insert the new one) committed during a publish
waits for that commit and then succeeds. The invoice uses exactly one set of
rules, those valid at its tax point.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

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
def sessions(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed(sessions) -> tuple[int, int]:
    """Tax engine v2, a GB rule at 20%, and a draft invoice of two lines; ``(rule_id, invoice_id)``."""
    from app.models.commerce_invoice_ext import InvoiceLineKind
    from app.models.reseller import InvoicePurpose
    from app.services.invoice_service import InvoiceService, NewLine
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
        rule = add_rule(db, name="UK VAT 20%", jurisdiction="GB", source="pack", rounding_point="line")
        _user, account = retail_account(db, country="GB")
        invoice = InvoiceService.create(
            db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1500,
            currency="GBP", publish=False,
            lines=[NewLine("Server", 1000, kind=InvoiceLineKind.PRODUCT),
                   NewLine("Support", 500, kind=InvoiceLineKind.PRODUCT)],
        )
        db.commit()
        return rule.id, invoice.id


def _supersede(db, rule_id: int, at: datetime, rate: str) -> int:
    """What the supersede route does: lock the old row, close it, insert the new one."""
    from app.models.tax import TaxRule

    old = db.execute(sa.select(TaxRule).where(TaxRule.id == rule_id).with_for_update()).scalar_one()
    old.valid_to = at
    db.flush()
    values = {c.name: getattr(old, c.name) for c in TaxRule.__table__.columns
              if c.name not in ("id", "created_at", "updated_at", "valid_to")}
    values.update(rate_percent=rate, valid_from=at, supersedes_id=old.id, name=f"UK VAT {rate}%")
    new = TaxRule(**values)
    db.add(new)
    db.commit()
    return new.id


def _rows(sessions, invoice_id: int) -> list[tuple]:
    from app.models.tax import InvoiceLineTax

    with sessions() as db:
        return [
            (row.tax_rule_id, str(row.rate_percent), row.tax_cents)
            for row in db.scalars(
                sa.select(InvoiceLineTax).where(InvoiceLineTax.invoice_id == invoice_id).order_by(InvoiceLineTax.id)
            )
        ]


def test_supersede_during_publish_waits_only_for_its_commit_and_never_mixes_rules(sessions):
    """The publish never locks a rule row for update; the foreign key check of its
    ``invoice_line_taxes.tax_rule_id`` insert holds InnoDB's shared lock on the
    rule row until it commits, so a concurrent supersede waits for that commit
    and then succeeds (no deadlock: the supersede holds no billing lock).
    """
    import threading

    from app.models.reseller import Invoice
    from app.models.tax import TaxRule
    from app.services.invoice_service import InvoiceService

    rule_id, invoice_id = _seed(sessions)
    publisher = sessions()
    outcome: dict = {}

    def supersede():
        with sessions() as other:
            other.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 20"))
            outcome["new_id"] = _supersede(other, rule_id, datetime.now(timezone.utc) + timedelta(seconds=1), "25")

    try:
        invoice = publisher.get(Invoice, invoice_id)
        InvoiceService.publish(publisher, invoice)
        # The publish's transaction is open: the rule row may be read and share-locked, not locked for update.
        with sessions() as reader:
            reader.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 2"))
            shared = reader.execute(
                sa.select(TaxRule.id).where(TaxRule.id == rule_id).with_for_update(read=True)
            ).scalar_one()
            reader.rollback()
        thread = threading.Thread(target=supersede)
        thread.start()
        thread.join(timeout=1)
        assert thread.is_alive(), "the supersede did not wait for the publish's foreign key lock"
        publisher.commit()
        thread.join(timeout=20)
        assert not thread.is_alive()
    finally:
        publisher.close()
    assert shared == rule_id and outcome["new_id"] != rule_id
    assert _rows(sessions, invoice_id) == [(rule_id, "20.0000", 200), (rule_id, "20.0000", 100)]


def test_publish_after_a_committed_supersede_uses_only_the_new_rule(sessions):
    from app.models.reseller import Invoice
    from app.services.invoice_service import InvoiceService

    rule_id, invoice_id = _seed(sessions)
    with sessions() as other:
        new_id = _supersede(other, rule_id, datetime.now(timezone.utc) - timedelta(seconds=1), "25")
    with sessions() as db:
        InvoiceService.publish(db, db.get(Invoice, invoice_id))
        db.commit()
    assert _rows(sessions, invoice_id) == [(new_id, "25.0000", 250), (new_id, "25.0000", 125)]
