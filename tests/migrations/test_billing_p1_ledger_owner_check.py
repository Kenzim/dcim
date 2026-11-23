"""Revision ``billing_p1_ledger_owner_check``: an entry needs a reseller or an account."""

import pytest
import sqlalchemy as sa

from tests.migrations import helpers
from tests.migrations.helpers import execute

REVISION = "billing_p1_ledger_owner_check"
TABLE = "credit_ledger_entries"


def _entry(engine, key, *, reseller_id=None, billing_account_id=None):
    execute(
        engine,
        f"INSERT INTO {TABLE} (reseller_id, billing_account_id, entry_type, amount_cents, "
        "balance_after_cents, idempotency_key, metadata) VALUES (:r, :a, 'topup', 5, 5, :k, '{}')",
        r=reseller_id, a=billing_account_id, k=key,
    )


def test_chain_position_and_head():
    directory = helpers.script()
    assert directory.get_revision(REVISION).down_revision == helpers.before(REVISION)
    assert REVISION == helpers.P1_HEAD
    assert len(directory.get_heads()) == 1


def test_owner_check_refuses_entries_without_owner(engine_at):
    engine = engine_at(helpers.before(REVISION))
    _entry(engine, "orphan-before")
    execute(engine, f"DELETE FROM {TABLE}")
    _entry(engine, "reseller", reseller_id=1, billing_account_id=2)
    indexes_before = helpers.sqlite_indexes(engine, TABLE)
    keys_before = helpers.sqlite_foreign_keys(engine, TABLE)

    helpers.migrate(engine, REVISION)
    assert "ck_credit_ledger_owner" in helpers.sqlite_table_sql(engine, TABLE)
    assert helpers.sqlite_indexes(engine, TABLE) == indexes_before
    assert helpers.sqlite_foreign_keys(engine, TABLE) == keys_before
    _entry(engine, "retail", billing_account_id=3)
    with pytest.raises(sa.exc.IntegrityError):
        _entry(engine, "orphan")

    helpers.migrate(engine, helpers.before(REVISION))
    assert "ck_credit_ledger_owner" not in helpers.sqlite_table_sql(engine, TABLE)
    assert helpers.sqlite_indexes(engine, TABLE) == indexes_before
    _entry(engine, "orphan-after-downgrade")
    execute(engine, f"DELETE FROM {TABLE} WHERE reseller_id IS NULL AND billing_account_id IS NULL")
    helpers.migrate(engine, REVISION)
    assert helpers.current(engine) == REVISION
