"""Revision ``billing_p1_adjustments``: credit and debit note tables."""

import pytest
import sqlalchemy as sa

from tests.migrations import helpers
from tests.migrations.helpers import execute

REVISION = "billing_p1_adjustments"


def _note(engine, key, **overrides):
    values = {
        "kind": "credit", "invoice_id": 1, "billing_account_id": 1, "reseller_id": None,
        "series_id": 3, "number_value": 1, "number_display": f"CN-{key}", "currency": "USD",
        "subtotal_cents": 100, "tax_cents": 0, "total_cents": 100, "reason_code": "goodwill",
        "outcome": "reduced_balance", "idempotency_key": key, "metadata": "{}",
    }
    values.update(overrides)
    names = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    execute(
        engine,
        f"INSERT INTO invoice_adjustments ({names}, issued_at) VALUES ({params}, CURRENT_TIMESTAMP)",
        **values,
    )


def test_chain_position():
    assert helpers.script().get_revision(REVISION).down_revision == helpers.before(REVISION)


def test_upgrade_creates_tables_with_checks(engine_at):
    engine = engine_at(REVISION)
    assert {"invoice_adjustments", "invoice_adjustment_lines"} <= helpers.tables(engine)
    _note(engine, "k1")
    _note(engine, "k2", number_value=2, kind="debit", outcome="credited_to_balance", reason_code="overpayment")
    refused = [
        {"billing_account_id": None},
        {"total_cents": 0, "subtotal_cents": 0},
        {"total_cents": 101},
        {"kind": "debit", "outcome": "refunded"},
        {"kind": "debit", "outcome": "credited_to_balance", "reason_code": "goodwill"},
        {"number_value": 1, "number_display": "CN-other"},
    ]
    for index, overrides in enumerate(refused):
        values = {"number_value": 100 + index, **overrides}
        with pytest.raises(sa.exc.IntegrityError):
            _note(engine, f"bad-{index}", **values)
    with pytest.raises(sa.exc.IntegrityError):
        _note(engine, "k1", number_value=50, number_display="CN-50")
    with pytest.raises(sa.exc.IntegrityError):
        execute(
            engine,
            "INSERT INTO invoice_adjustment_lines (adjustment_id, kind, description, quantity, unit_cents, "
            "total_cents) VALUES (1, 'product', 'x', 0, 1, 0)",
        )


def test_downgrade_removes_and_upgrade_again(engine_at):
    engine = engine_at(REVISION)
    helpers.migrate(engine, helpers.before(REVISION))
    assert not {"invoice_adjustments", "invoice_adjustment_lines"} & helpers.tables(engine)
    helpers.migrate(engine, REVISION)
    assert {"invoice_adjustments", "invoice_adjustment_lines"} <= helpers.tables(engine)
