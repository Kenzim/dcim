"""Revision ``billing_p1_invoice_columns``: v2 columns on invoices and invoice_lines."""

import pytest
import sqlalchemy as sa

from tests.migrations import helpers
from tests.migrations.helpers import execute, scalar

REVISION = "billing_p1_invoice_columns"
INVOICE_COLUMNS = {
    "model_version", "document_type", "subtotal_cents", "discount_cents", "tax_cents",
    "prices_include_tax", "issued_at", "seller_snapshot", "buyer_snapshot", "customer_notes",
    "admin_notes", "locale", "series_id", "number_period", "number_value", "number_display",
    "proforma_series_id", "proforma_number_period", "proforma_number_value",
    "proforma_number_display", "status_changed_at", "voided_at", "void_reason",
}
LINE_COLUMNS = {
    "kind", "service_id", "service_billing_id", "period_start", "period_end", "tax_class_id",
    "taxable", "tax_snapshot",
}


def _invoice(engine, number, **values):
    columns = {"invoice_number": number, "purpose": "deploy", "amount_cents": 100, **values}
    names = ", ".join(columns)
    params = ", ".join(f":{name}" for name in columns)
    execute(engine, f"INSERT INTO invoices ({names}) VALUES ({params})", **columns)


def test_chain_position():
    assert helpers.script().get_revision(REVISION).down_revision == helpers.before(REVISION)


def test_upgrade_adds_columns_and_keeps_rows(engine_at):
    engine = engine_at(helpers.before(REVISION))
    _invoice(engine, 1001)
    helpers.migrate(engine, REVISION)
    assert INVOICE_COLUMNS <= helpers.columns(engine, "invoices")
    assert LINE_COLUMNS <= helpers.columns(engine, "invoice_lines")
    assert scalar(engine, "SELECT document_type FROM invoices WHERE invoice_number = 1001") == "invoice"
    assert scalar(engine, "SELECT model_version FROM invoices WHERE invoice_number = 1001") is None
    names = {name for name, _unique, _cols in helpers.sqlite_indexes(engine, "invoices")}
    assert {
        "uq_invoices_series_number", "uq_invoices_series_display", "uq_invoices_proforma_number",
        "ix_invoices_series_id", "ix_invoices_account_status_due",
    } <= names
    line_names = {name for name, _unique, _cols in helpers.sqlite_indexes(engine, "invoice_lines")}
    assert {"ix_invoice_lines_service_id", "ix_invoice_lines_service_billing_id"} <= line_names


def test_series_numbers_are_unique_and_nulls_do_not_collide(engine_at):
    engine = engine_at(REVISION)
    _invoice(engine, 1)
    _invoice(engine, 2)
    _invoice(engine, 3, series_id=1, number_period="", number_value=7, number_display="7")
    with pytest.raises(sa.exc.IntegrityError):
        _invoice(engine, 4, series_id=1, number_period="", number_value=7, number_display="7b")
    with pytest.raises(sa.exc.IntegrityError):
        _invoice(engine, 5, series_id=1, number_period="", number_value=8, number_display="7")
    _invoice(engine, 6, series_id=1, number_period="2027", number_value=7, number_display="2027-7")


def test_downgrade_removes_and_upgrade_again(engine_at):
    engine = engine_at(REVISION)
    helpers.migrate(engine, helpers.before(REVISION))
    assert not INVOICE_COLUMNS & helpers.columns(engine, "invoices")
    assert not LINE_COLUMNS & helpers.columns(engine, "invoice_lines")
    helpers.migrate(engine, REVISION)
    assert INVOICE_COLUMNS <= helpers.columns(engine, "invoices")
