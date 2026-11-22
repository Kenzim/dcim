"""Revision ``billing_p1_refunds``: refund columns on payments and the refunds table."""

import pytest
import sqlalchemy as sa

from tests.migrations import helpers
from tests.migrations.helpers import execute, scalar

REVISION = "billing_p1_refunds"
PAYMENT_COLUMNS = {"refunded_cents", "fee_cents", "net_cents"}


def _refund(engine, key, **overrides):
    values = {
        "payment_id": 1, "invoice_id": 1, "billing_account_id": 1, "amount_cents": 50,
        "currency": "USD", "destination": "gateway", "gateway": "stripe",
        "reason": "customer_request", "idempotency_key": key, "metadata": "{}",
    }
    values.update(overrides)
    names = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    execute(
        engine,
        f"INSERT INTO refunds ({names}, requested_at) VALUES ({params}, CURRENT_TIMESTAMP)",
        **values,
    )


def test_chain_position():
    assert helpers.script().get_revision(REVISION).down_revision == helpers.before(REVISION)


def test_upgrade_adds_payment_columns_with_zero_default(engine_at):
    engine = engine_at(helpers.before(REVISION))
    execute(
        engine,
        "INSERT INTO payments (invoice_id, gateway, status, amount_cents, metadata) "
        "VALUES (1, 'stripe', 'succeeded', 500, '{}')",
    )
    helpers.migrate(engine, REVISION)
    assert PAYMENT_COLUMNS <= helpers.columns(engine, "payments")
    assert scalar(engine, "SELECT refunded_cents FROM payments") == 0
    assert scalar(engine, "SELECT fee_cents FROM payments") is None


def test_refunds_table_checks(engine_at):
    engine = engine_at(REVISION)
    _refund(engine, "r1", gateway_refund_ref="re_1")
    assert scalar(engine, "SELECT status FROM refunds") == "pending"
    assert scalar(engine, "SELECT issue_credit_note FROM refunds") == 1
    for index, overrides in enumerate(
        [{"amount_cents": 0}, {"billing_account_id": None}, {"gateway_refund_ref": "re_1"}]
    ):
        with pytest.raises(sa.exc.IntegrityError):
            _refund(engine, f"bad-{index}", **overrides)
    with pytest.raises(sa.exc.IntegrityError):
        _refund(engine, "r1")


def test_downgrade_removes_and_upgrade_again(engine_at):
    engine = engine_at(REVISION)
    helpers.migrate(engine, helpers.before(REVISION))
    assert "refunds" not in helpers.tables(engine)
    assert not PAYMENT_COLUMNS & helpers.columns(engine, "payments")
    helpers.migrate(engine, REVISION)
    assert "refunds" in helpers.tables(engine)
