"""A tax rule superseded during a publish, on MariaDB (optional; design 4.9, 15.5).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards.

The engine reads the rules with plain reads. Before its first
``invoice_line_taxes`` insert the publish share-locks the rules it uses with
one locking read in id order (``invoice_tax.lock_rules``), which returns the
latest committed rows, and refuses with ``TaxRuleChanged`` when one was closed
or superseded by the tax point. A supersede (lock the old row, close it,
insert the new one) committed during a publish waits for that publish to
commit. The invoice uses exactly one set of rules, those valid at its tax
point.
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
    """The publish never locks a rule row for update; its share lock on the rule
    row is held until it commits, so a concurrent supersede waits for that
    commit and then succeeds (no deadlock: the supersede holds no billing lock).
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


def _repeatable_read(db) -> bool:
    return db.execute(sa.text("SELECT @@tx_isolation")).scalar_one() == "REPEATABLE-READ"


def _status(sessions, invoice_id: int) -> str:
    from app.models.reseller import Invoice

    with sessions() as db:
        return db.get(Invoice, invoice_id).status.value


def test_supersede_committed_after_the_publisher_read_the_rule_never_freezes_it(sessions):
    """The reviewer's repro: the publisher reads the rule (its snapshot), another
    connection commits a supersede valid from now, then the publish runs. Under
    REPEATABLE READ the engine still sees the old rule, so the publish raises
    ``TaxRuleChanged`` and a retry in a new transaction uses the new rule; under
    READ COMMITTED the engine already sees the new rule.
    """
    from app.models.reseller import Invoice
    from app.models.tax import TaxRule
    from app.services.invoice_service import InvoiceService
    from app.services.tax.invoice_tax import TaxRuleChanged

    rule_id, invoice_id = _seed(sessions)
    publisher = sessions()
    try:
        repeatable = _repeatable_read(publisher)
        assert publisher.execute(sa.select(TaxRule.valid_to).where(TaxRule.id == rule_id)).scalar_one() is None
        with sessions() as other:
            new_id = _supersede(other, rule_id, datetime.now(timezone.utc), "25")
        invoice = publisher.get(Invoice, invoice_id)
        if repeatable:
            with pytest.raises(TaxRuleChanged) as raised:
                InvoiceService.publish(publisher, invoice)
            assert raised.value.rule_ids == (rule_id,)
            publisher.rollback()
            assert _status(sessions, invoice_id) == "draft"
            with sessions() as retry:
                InvoiceService.publish(retry, retry.get(Invoice, invoice_id))
                retry.commit()
        else:
            InvoiceService.publish(publisher, invoice)
            publisher.commit()
    finally:
        publisher.close()
    assert _status(sessions, invoice_id) == "open"
    assert _rows(sessions, invoice_id) == [(new_id, "25.0000", 250), (new_id, "25.0000", 125)]


def test_the_error_leaves_a_savepoint_cleanly(sessions):
    """Raised inside ``begin_nested`` (as ``_ensure_cycle`` creates a cycle invoice),
    the error reaches the caller as itself, not as MariaDB error 1305, and the
    transaction can still be rolled back and reused.
    """
    from app.models.reseller import Invoice
    from app.models.tax import TaxRule
    from app.services.invoice_service import InvoiceService
    from app.services.tax.invoice_tax import TaxRuleChanged

    rule_id, invoice_id = _seed(sessions)
    with sessions() as publisher:
        publisher.execute(sa.select(TaxRule.valid_to).where(TaxRule.id == rule_id)).scalar_one()
        with sessions() as other:
            _supersede(other, rule_id, datetime.now(timezone.utc), "25")
        expected = TaxRuleChanged if _repeatable_read(publisher) else type(None)
        caught = None
        try:
            with publisher.begin_nested():
                InvoiceService.publish(publisher, publisher.get(Invoice, invoice_id))
        except Exception as exc:  # noqa: BLE001 - the type is asserted below
            caught = exc
        assert type(caught) is expected
        publisher.rollback()
        assert publisher.execute(sa.text("SELECT 1")).scalar_one() == 1


def _seed_many(sessions) -> tuple[list[int], int]:
    """Three GB rules (one per tax class) among 600 pack rules whose keys sort against their ids.

    Returns the ids of the GB rules and a draft invoice with a line in each class.
    """
    from app.models.commerce_invoice_ext import InvoiceLineKind
    from app.models.reseller import InvoicePurpose
    from app.models.tax import TaxRule
    from app.services.invoice_service import InvoiceService, NewLine
    from tests.services.tax_helpers import (
        EPOCH,
        add_registration,
        add_rule,
        enable_tax,
        invoice_v2,
        retail_account,
        seed_classes,
        set_engine,
    )

    used = []
    with sessions() as db:
        classes = seed_classes(db)
        invoice_v2(db)
        set_engine(db, "tax", "v2")
        enable_tax(db, "GB")
        add_registration(db, "GB")
        countries = ["DE", "FR", "IT", "ES", "NL", "BE"]
        for block, (code, rate) in enumerate((("standard", "20"), ("reduced", "5"), ("zero", "0"))):
            db.execute(sa.insert(TaxRule), [
                {"name": f"EU {n}", "country": countries[n % 6], "tax_class_id": classes["standard"],
                 "rate_percent": "19", "rounding_rule": "round_half_up", "valid_from": EPOCH,
                 "pack_id": "eu_vat", "pack_version": "1", "pack_rule_key": f"k{9999 - block * 200 - n:04d}"}
                for n in range(200)
            ])
            used.append(add_rule(
                db, name=f"UK VAT {rate}%", rate=rate, tax_class=code, jurisdiction="GB", source="pack",
                rounding_point="line", pack_id="eu_vat", pack_version="1", pack_rule_key=f"a{9 - block}",
            ).id)
        _user, account = retail_account(db, country="GB")
        invoice = InvoiceService.create(
            db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=3000,
            currency="GBP", publish=False,
            lines=[NewLine(f"Line {code}", 1000, kind=InvoiceLineKind.PRODUCT, tax_class_id=classes[code])
                   for code in ("zero", "reduced", "standard")],
        )
        db.commit()
        db.execute(sa.text("ANALYZE TABLE tax_rules"))
        return sorted(used), invoice.id


def _publish_in_thread(sessions, invoice_id: int, outcome: dict, *, commit: bool):
    import threading

    from app.models.reseller import Invoice
    from app.services.invoice_service import InvoiceService

    def run():
        with sessions() as db:
            db.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 20"))
            try:
                InvoiceService.publish(db, db.get(Invoice, invoice_id))
                if commit:
                    db.commit()
                outcome["result"] = "published"
            except Exception as exc:  # noqa: BLE001 - reported to the test
                outcome["result"] = exc
                db.rollback()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _try_lock(sessions, rule_id: int) -> bool:
    """Whether another connection can lock ``rule_id`` for update at once."""
    from app.models.tax import TaxRule

    with sessions() as probe:
        try:
            probe.execute(sa.select(TaxRule.id).where(TaxRule.id == rule_id).with_for_update(nowait=True)).all()
            return True
        except sa.exc.OperationalError:
            return False
        finally:
            probe.rollback()


def test_the_rule_lock_reads_by_primary_key_among_many_pack_rules(sessions):
    from app.services.tax.invoice_tax import rule_lock_statement

    used, _invoice_id = _seed_many(sessions)
    with sessions() as db:
        statement = rule_lock_statement(used)
        sql = str(statement.compile(dialect=db.get_bind().dialect, compile_kwargs={"literal_binds": True}))
        assert "FORCE INDEX (PRIMARY)" in sql and "LOCK IN SHARE MODE" in sql
        plan = db.execute(sa.text(f"EXPLAIN {sql}")).mappings().all()
        assert [row["key"] for row in plan] == ["PRIMARY"]


def test_publish_takes_the_rule_locks_in_ascending_id_order(sessions):
    """With the highest rule held, the publish already holds the lowest; with the
    lowest held, it holds nothing yet. So the locks are taken in id order.
    """
    import time

    from app.models.tax import TaxRule

    used, invoice_id = _seed_many(sessions)
    low, high = used[0], used[-1]
    for held, probe, publish_holds_probe in ((high, low, True), (low, high, False)):
        holder = sessions()
        try:
            holder.execute(sa.select(TaxRule.id).where(TaxRule.id == held).with_for_update()).all()
            outcome: dict = {}
            thread = _publish_in_thread(sessions, invoice_id, outcome, commit=False)
            time.sleep(1.5)
            assert thread.is_alive(), outcome
            assert _try_lock(sessions, probe) is (not publish_holds_probe)
        finally:
            holder.rollback()
            holder.close()
        thread.join(timeout=20)
        assert not thread.is_alive() and outcome["result"] == "published"


def test_an_import_holding_the_rules_in_descending_order_makes_the_publish_wait(sessions):
    """An import that has locked and closed the rules (taken high to low) and
    inserted their successors: the publish waits for it, never deadlocks, and
    then refuses with ``TaxRuleChanged``; the retry uses the new rules.
    """
    import time

    from app.models.tax import TaxRule
    from app.services.tax.invoice_tax import TaxRuleChanged

    used, invoice_id = _seed_many(sessions)
    importer = sessions()
    new_ids = []
    try:
        at = datetime.now(timezone.utc)
        for rule_id in reversed(used):
            old = importer.execute(sa.select(TaxRule).where(TaxRule.id == rule_id).with_for_update()).scalar_one()
            old.valid_to = at
            importer.flush()
            values = {c.name: getattr(old, c.name) for c in TaxRule.__table__.columns
                      if c.name not in ("id", "created_at", "updated_at", "valid_to")}
            new = TaxRule(**{**values, "valid_from": at, "supersedes_id": old.id, "pack_version": "2"})
            importer.add(new)
            importer.flush()
            new_ids.append(new.id)
        outcome: dict = {}
        thread = _publish_in_thread(sessions, invoice_id, outcome, commit=True)
        time.sleep(1.5)
        assert thread.is_alive(), outcome
        importer.commit()
    finally:
        importer.close()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert isinstance(outcome["result"], TaxRuleChanged), outcome
    assert outcome["result"].rule_ids == tuple(used)
    outcome = {}
    _publish_in_thread(sessions, invoice_id, outcome, commit=True).join(timeout=20)
    assert outcome["result"] == "published"
    assert sorted({row[0] for row in _rows(sessions, invoice_id)}) <= sorted(new_ids)
    assert _status(sessions, invoice_id) == "open"
