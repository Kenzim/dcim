"""Revision ``billing_p1_ledger_accounts``: billing account on ledger entries, reseller optional."""

import pytest
import sqlalchemy as sa

from tests.migrations import helpers
from tests.migrations.helpers import execute, scalar

REVISION = "billing_p1_ledger_accounts"
TABLE = "credit_ledger_entries"


def _entry(engine, key, *, reseller_id, billing_account_id=None, amount=100):
    columns = {
        "reseller_id": reseller_id, "entry_type": "topup", "amount_cents": amount,
        "balance_after_cents": amount, "idempotency_key": key, "metadata": "{}",
    }
    if billing_account_id is not None:
        columns["billing_account_id"] = billing_account_id
    names = ", ".join(columns)
    params = ", ".join(f":{name}" for name in columns)
    execute(engine, f"INSERT INTO {TABLE} ({names}) VALUES ({params})", **columns)


def test_chain_position():
    assert helpers.script().get_revision(REVISION).down_revision == helpers.before(REVISION)


def test_upgrade_keeps_rows_indexes_and_foreign_keys(engine_at):
    engine = engine_at(helpers.before(REVISION))
    _entry(engine, "old-1", reseller_id=7)
    with pytest.raises(sa.exc.IntegrityError):
        _entry(engine, "retail-before", reseller_id=None)
    indexes_before = helpers.sqlite_indexes(engine, TABLE)
    keys_before = helpers.sqlite_foreign_keys(engine, TABLE)

    helpers.migrate(engine, REVISION)
    assert "billing_account_id" in helpers.columns(engine, TABLE)
    assert helpers.sqlite_indexes(engine, TABLE) == indexes_before | {
        ("ix_credit_ledger_account_id", False, ("billing_account_id", "id"))
    }
    assert helpers.sqlite_foreign_keys(engine, TABLE) == keys_before | {
        ("billing_accounts", "billing_account_id", "id", "RESTRICT")
    }
    assert "ck_credit_ledger_nonzero_amount" in helpers.sqlite_table_sql(engine, TABLE)
    assert scalar(engine, f"SELECT reseller_id FROM {TABLE} WHERE idempotency_key = 'old-1'") == 7

    _entry(engine, "retail-1", reseller_id=None, billing_account_id=3)
    with pytest.raises(sa.exc.IntegrityError):
        _entry(engine, "zero", reseller_id=None, billing_account_id=3, amount=0)
    with pytest.raises(sa.exc.IntegrityError):
        _entry(engine, "old-1", reseller_id=7)


def test_downgrade_refuses_retail_entries_then_restores(engine_at):
    engine = engine_at(helpers.before(REVISION))
    indexes_before = helpers.sqlite_indexes(engine, TABLE)
    keys_before = helpers.sqlite_foreign_keys(engine, TABLE)
    helpers.migrate(engine, REVISION)
    _entry(engine, "retail-1", reseller_id=None, billing_account_id=3)
    with pytest.raises(RuntimeError, match="retail entries"):
        helpers.migrate(engine, helpers.before(REVISION))
    execute(engine, f"DELETE FROM {TABLE} WHERE reseller_id IS NULL")

    helpers.migrate(engine, helpers.before(REVISION))
    assert "billing_account_id" not in helpers.columns(engine, TABLE)
    assert helpers.sqlite_indexes(engine, TABLE) == indexes_before
    assert helpers.sqlite_foreign_keys(engine, TABLE) == keys_before
    with pytest.raises(sa.exc.IntegrityError):
        _entry(engine, "retail-2", reseller_id=None)
    helpers.migrate(engine, REVISION)
    assert "billing_account_id" in helpers.columns(engine, TABLE)
