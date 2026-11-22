"""Revision ``billing_p1_invoice_checks``: zero-amount invoices and the v2 checks."""

import pytest
import sqlalchemy as sa

from tests.migrations import helpers
from tests.migrations.helpers import execute

REVISION = "billing_p1_invoice_checks"


def _invoice(engine, number, amount, **values):
    columns = {"invoice_number": number, "purpose": "deploy", "amount_cents": amount, **values}
    names = ", ".join(columns)
    params = ", ".join(f":{name}" for name in columns)
    execute(engine, f"INSERT INTO invoices ({names}) VALUES ({params})", **columns)


def test_chain_position():
    assert helpers.script().get_revision(REVISION).down_revision == helpers.before(REVISION)


def test_zero_amount_refused_before_accepted_after_refused_after_downgrade(engine_at):
    engine = engine_at(helpers.before(REVISION))
    _invoice(engine, 1, 500)
    with pytest.raises(sa.exc.IntegrityError):
        _invoice(engine, 2, 0)
    indexes_before = helpers.sqlite_indexes(engine, "invoices")
    keys_before = helpers.sqlite_foreign_keys(engine, "invoices")

    helpers.migrate(engine, REVISION)
    sql = helpers.sqlite_table_sql(engine, "invoices")
    assert "amount_cents >= 0" in sql and "amount_cents > 0" not in sql
    assert {"ck_invoices_amount_nonnegative", "ck_invoices_v2_totals", "ck_invoices_v2_number"} <= set(
        sql.replace("CONSTRAINT ", "\n").split()
    )
    assert helpers.sqlite_indexes(engine, "invoices") == indexes_before
    assert helpers.sqlite_foreign_keys(engine, "invoices") == keys_before
    _invoice(engine, 2, 0)
    with pytest.raises(sa.exc.IntegrityError):
        _invoice(engine, 3, -1)

    with pytest.raises(RuntimeError, match="amount_cents <= 0"):
        helpers.migrate(engine, helpers.before(REVISION))
    execute(engine, "DELETE FROM invoices WHERE amount_cents = 0")
    helpers.migrate(engine, helpers.before(REVISION))
    assert "amount_cents > 0" in helpers.sqlite_table_sql(engine, "invoices")
    assert helpers.sqlite_indexes(engine, "invoices") == indexes_before
    assert helpers.sqlite_foreign_keys(engine, "invoices") == keys_before
    with pytest.raises(sa.exc.IntegrityError):
        _invoice(engine, 4, 0)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT amount_cents FROM invoices WHERE invoice_number = 1")).scalar() == 500

    helpers.migrate(engine, REVISION)
    _invoice(engine, 5, 0)


def test_v2_checks(engine_at):
    engine = engine_at(REVISION)
    _invoice(engine, 1, 100, model_version=None)
    _invoice(engine, 2, 100, model_version=2, status="draft", subtotal_cents=100, discount_cents=0,
             tax_cents=0, prices_include_tax=False)
    _invoice(engine, 3, 100, model_version=2, status="open", subtotal_cents=100, discount_cents=0,
             tax_cents=0, prices_include_tax=False, number_display="3")
    _invoice(engine, 4, 100, model_version=2, status="open", subtotal_cents=100, discount_cents=0,
             tax_cents=0, prices_include_tax=False, proforma_number_display="PF-1")
    with pytest.raises(sa.exc.IntegrityError):
        _invoice(engine, 5, 100, model_version=2, status="draft", subtotal_cents=100)
    with pytest.raises(sa.exc.IntegrityError):
        _invoice(engine, 6, 100, model_version=2, status="open", subtotal_cents=100, discount_cents=0,
                 tax_cents=0, prices_include_tax=False)
