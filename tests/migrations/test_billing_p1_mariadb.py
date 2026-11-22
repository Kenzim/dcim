"""Billing phase 1 chain on MariaDB 11 (optional; skipped unless a server is given).

Set ``RACKFLOW_TEST_MARIADB_URL`` to an empty database, for example
``mysql+pymysql://root:pw@127.0.0.1:3307/rackflow``. The test drops and
recreates every table in it and drops them again afterwards, so it refuses a
database that holds rows in ``users`` or ``invoices`` unless
``RACKFLOW_TEST_MARIADB_ALLOW_DROP=1``.
``.forgejo/workflows/billing-mariadb.yml`` runs it against a ``mariadb:11.8``
service container.

A fresh ``alembic upgrade`` cannot reach ``billing_a0_renewals`` on MariaDB
(``add_rack_units_servers`` lacks ``existing_type``, ledger to-do e01f7dbc), so
the schema is created from the models, stamped at the phase 1 head, and taken
down to ``billing_a0_renewals`` through the real phase 1 downgrades; then the
chain is driven up, down and up again.
"""

import os

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext

from tests.migrations import helpers

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")


def _refuse_populated_database(conn) -> None:
    if os.environ.get("RACKFLOW_TEST_MARIADB_ALLOW_DROP") == "1":
        return
    tables = set(sa.inspect(conn).get_table_names())
    for table in ("users", "invoices"):
        if table in tables and conn.execute(sa.text(f"SELECT 1 FROM `{table}` LIMIT 1")).first():
            pytest.fail(
                f"RACKFLOW_TEST_MARIADB_URL points at a database with rows in {table}; "
                "use an empty throwaway database or set RACKFLOW_TEST_MARIADB_ALLOW_DROP=1"
            )


@pytest.fixture
def mariadb():
    import app.models  # noqa: F401
    from app.core.database import Base, configure_engine

    engine = configure_engine(sa.create_engine(URL, pool_pre_ping=True))
    with engine.begin() as conn:
        _refuse_populated_database(conn)
    _drop_all(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        MigrationContext.configure(conn).stamp(helpers.script(), helpers.HEAD)
    helpers.migrate(engine, helpers.A0)
    yield engine
    _drop_all(engine)
    engine.dispose()


def _drop_all(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("SET foreign_key_checks = 0"))
        for table in sa.inspect(conn).get_table_names():
            conn.execute(sa.text(f"DROP TABLE `{table}`"))
        conn.execute(sa.text("SET foreign_key_checks = 1"))


def _checks(engine, table):
    with engine.connect() as conn:
        return {
            name
            for (name,) in conn.execute(
                sa.text(
                    "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS WHERE "
                    "TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND CONSTRAINT_TYPE = 'CHECK'"
                ),
                {"t": table},
            )
        }


def _insert_invoice(engine, number, amount):
    helpers.execute(
        engine,
        "INSERT INTO invoices (invoice_number, purpose, status, amount_cents, currency) "
        "VALUES (:n, 'deploy', 'open', :a, 'USD')",
        n=number, a=amount,
    )


def test_chain_up_down_up_on_mariadb(mariadb):
    engine = mariadb
    assert helpers.current(engine) == helpers.A0
    assert "ck_invoices_positive_amount" in _checks(engine, "invoices")
    assert not helpers.NEW_TABLES & helpers.tables(engine)

    helpers.execute(engine, "INSERT INTO users (id, username, email, is_admin) VALUES (1, 'r', 'r@x', 0)")
    helpers.execute(engine, "INSERT INTO resellers (id, user_id, cached_balance_cents) VALUES (1, 1, 700)")
    helpers.execute(
        engine,
        "INSERT INTO credit_ledger_entries (reseller_id, entry_type, amount_cents, balance_after_cents, metadata) "
        "VALUES (1, 'topup', 700, 700, '{}')",
    )
    _insert_invoice(engine, 1, 500)
    helpers.execute(
        engine,
        "INSERT INTO payments (invoice_id, gateway, status, amount_cents, currency, metadata) "
        "VALUES (1, 'stripe', 'refunded', 500, 'USD', '{}')",
    )
    with pytest.raises(sa.exc.DBAPIError):
        _insert_invoice(engine, 2, 0)

    helpers.migrate(engine, helpers.P1_HEAD)
    assert helpers.current(engine) == helpers.P1_HEAD
    assert helpers.NEW_TABLES <= helpers.tables(engine)
    assert {"ck_invoices_amount_nonnegative", "ck_invoices_v2_totals", "ck_invoices_v2_number"} <= _checks(
        engine, "invoices"
    )
    assert "ck_invoices_positive_amount" not in _checks(engine, "invoices")
    assert "ck_credit_ledger_owner" in _checks(engine, "credit_ledger_entries")
    assert helpers.scalar(engine, "SELECT credit_balance_cents FROM billing_accounts WHERE reseller_id = 1") == 700
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM credit_ledger_entries WHERE billing_account_id IS NULL") == 0
    assert helpers.scalar(engine, "SELECT refunded_cents FROM payments") == 500
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM document_series") == 4
    _insert_invoice(engine, 2, 0)
    with pytest.raises(sa.exc.DBAPIError):
        helpers.execute(
            engine,
            "INSERT INTO credit_ledger_entries (entry_type, amount_cents, balance_after_cents, metadata) "
            "VALUES ('topup', 5, 5, '{}')",
        )

    with pytest.raises(RuntimeError, match="amount_cents <= 0"):
        helpers.migrate(engine, helpers.A0)
    helpers.execute(engine, "DELETE FROM invoices WHERE amount_cents = 0")
    helpers.migrate(engine, helpers.A0)
    assert helpers.current(engine) == helpers.A0
    assert "ck_invoices_positive_amount" in _checks(engine, "invoices")
    assert not helpers.NEW_TABLES & helpers.tables(engine)

    helpers.migrate(engine, helpers.P1_HEAD)
    assert helpers.current(engine) == helpers.P1_HEAD
