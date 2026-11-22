"""Revision ``billing_p1_backfill_accounts`` and its functions in ``billing_backfill``."""

import sqlalchemy as sa

from app.services import billing_backfill
from tests.migrations import helpers
from tests.migrations.helpers import execute, scalar

REVISION = "billing_p1_backfill_accounts"


def _seed(engine):
    """Five resellers (one already with an account), ledger entries, and retail accounts."""
    for reseller_id, balance in [(1, 500), (2, 0), (3, -200), (4, 1200), (5, 75)]:
        execute(
            engine,
            "INSERT INTO resellers (id, user_id, cached_balance_cents) VALUES (:id, :user_id, :balance)",
            id=reseller_id, user_id=100 + reseller_id, balance=balance,
        )
    execute(
        engine,
        "INSERT INTO billing_accounts (id, account_type, reseller_id, user_id, status, currency, "
        "credit_balance_cents, tax_exempt) VALUES (10, 'reseller', 4, 104, 'active', 'USD', 3, 0)",
    )
    for entry_id, reseller_id in [(1, 1), (2, 1), (3, 4), (4, 5), (5, 3)]:
        execute(
            engine,
            "INSERT INTO credit_ledger_entries (id, reseller_id, entry_type, amount_cents, "
            "balance_after_cents, metadata) VALUES (:id, :reseller, 'topup', 10, 10, '{}')",
            id=entry_id, reseller=reseller_id,
        )
    for account_id, balance in [(20, 900), (21, 0), (22, -40)]:
        execute(
            engine,
            "INSERT INTO billing_accounts (id, account_type, user_id, status, currency, "
            "credit_balance_cents, tax_exempt) VALUES (:id, 'client', :user_id, 'active', 'USD', :balance, 0)",
            id=account_id, user_id=200 + account_id, balance=balance,
        )


def _snapshot(engine):
    with engine.connect() as conn:
        return (
            conn.execute(sa.text(
                "SELECT id, account_type, reseller_id, user_id, status, currency, credit_balance_cents "
                "FROM billing_accounts ORDER BY id"
            )).all(),
            conn.execute(sa.text(
                "SELECT id, reseller_id, billing_account_id, entry_type, amount_cents, balance_after_cents, "
                "description, idempotency_key FROM credit_ledger_entries ORDER BY id"
            )).all(),
        )


def test_chain_position():
    assert helpers.script().get_revision(REVISION).down_revision == helpers.before(REVISION)


def test_upgrade_creates_syncs_links_and_opens(engine_at):
    engine = engine_at(helpers.before(REVISION))
    _seed(engine)
    helpers.migrate(engine, REVISION)

    accounts, entries = _snapshot(engine)
    reseller_accounts = {row.reseller_id: row for row in accounts if row.account_type == "reseller"}
    assert set(reseller_accounts) == {1, 2, 3, 4, 5}
    for reseller_id, balance in [(1, 500), (2, 0), (3, -200), (4, 1200), (5, 75)]:
        account = reseller_accounts[reseller_id]
        # Same fields as BillingAccountDAO.ensure_reseller_account.
        assert (account.user_id, account.status, account.currency) == (100 + reseller_id, "active", "USD")
        assert account.credit_balance_cents == balance
    assert reseller_accounts[4].id == 10

    linked = {row.id: row.billing_account_id for row in entries if row.reseller_id is not None}
    assert linked == {
        1: reseller_accounts[1].id, 2: reseller_accounts[1].id, 3: 10,
        4: reseller_accounts[5].id, 5: reseller_accounts[3].id,
    }
    opening = [row for row in entries if row.reseller_id is None]
    assert [(r.billing_account_id, r.entry_type, r.amount_cents, r.balance_after_cents, r.idempotency_key)
            for r in opening] == [
        (20, "adjustment", 900, 900, "a3-opening:20"),
        (22, "adjustment", -40, -40, "a3-opening:22"),
    ]
    assert opening[0].description == billing_backfill.OPENING_BALANCE_DESCRIPTION


def test_backfill_is_idempotent_and_batched(engine_at):
    engine = engine_at(helpers.before(REVISION))
    _seed(engine)
    with engine.begin() as conn:
        first = billing_backfill.backfill_accounts(conn, batch_size=2)
    assert [(r.name, r.batches) for r in first] == [
        ("reseller_accounts", 3),       # 5 resellers in batches of 2
        ("account_balances", 4),        # 8 accounts after step (a)
        ("ledger_accounts", 3),         # 5 entries
        ("opening_balances", 4),
    ]
    assert [r.rows for r in first] == [4, 1, 5, 2]
    after_first = _snapshot(engine)

    with engine.begin() as conn:
        second = billing_backfill.backfill_accounts(conn, batch_size=2)
    assert [r.rows for r in second] == [0, 0, 0, 0]
    assert _snapshot(engine) == after_first


def test_second_pass_catches_up_writes_made_while_the_deploy_rolls(engine_at):
    engine = engine_at(helpers.before(REVISION))
    _seed(engine)
    helpers.migrate(engine, REVISION)
    # An old process moves a reseller cache and writes an unlinked entry, and a
    # retail cache changes on an account that had no opening entry yet.
    execute(engine, "UPDATE resellers SET cached_balance_cents = 650 WHERE id = 1")
    execute(
        engine,
        "INSERT INTO credit_ledger_entries (reseller_id, entry_type, amount_cents, "
        "balance_after_cents, metadata) VALUES (1, 'topup', 150, 650, '{}')",
    )
    execute(engine, "UPDATE billing_accounts SET credit_balance_cents = 300 WHERE id = 21")

    with engine.begin() as conn:
        second = billing_backfill.backfill_accounts(conn)
    assert [r.rows for r in second] == [0, 1, 1, 1]
    assert scalar(
        engine,
        "SELECT credit_balance_cents FROM billing_accounts WHERE account_type = 'reseller' AND reseller_id = 1",
    ) == 650
    assert scalar(engine, "SELECT COUNT(*) FROM credit_ledger_entries WHERE billing_account_id IS NULL") == 0
    # The opening entry copies the cache as it is when the entry is written.
    assert scalar(
        engine, "SELECT amount_cents FROM credit_ledger_entries WHERE idempotency_key = 'a3-opening:21'"
    ) == 300


def test_command_line_commits_each_batch(engine_at, monkeypatch, capsys):
    engine = engine_at(REVISION)
    _seed(engine)
    import app.core.database as database

    monkeypatch.setattr(database, "engine", engine)
    assert billing_backfill.main(["ledger_accounts", "--batch-size", "2"]) == 0
    # Only reseller 4 has an account yet; the other entries wait for step (a).
    assert "ledger_accounts: 1 rows in 3 batches" in capsys.readouterr().out
    assert billing_backfill.main(["accounts", "--batch-size", "2"]) == 0
    assert "ledger_accounts: 4 rows in 3 batches" in capsys.readouterr().out
    assert scalar(engine, "SELECT COUNT(*) FROM credit_ledger_entries WHERE billing_account_id IS NULL") == 0


def test_downgrade_keeps_data_and_upgrade_again(engine_at):
    engine = engine_at(REVISION)
    helpers.migrate(engine, helpers.before(REVISION))
    assert helpers.current(engine) == helpers.before(REVISION)
    helpers.migrate(engine, REVISION)
    assert helpers.current(engine) == REVISION
