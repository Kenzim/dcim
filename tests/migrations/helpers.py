"""Shared helpers for the billing phase 1 migration tests.

A fresh ``alembic upgrade`` from an empty database does not work on SQLite
(older revisions use MySQL-only DDL) nor on MariaDB (``add_rack_units_servers``
lacks ``existing_type``, ledger to-do e01f7dbc). The SQLite tests therefore load
``billing_a0_schema.sql``, the tables the phase 1 chain touches as they stood at
``billing_a0_renewals`` (generated from the models at commit ca24629), stamp
that revision, and drive the real phase 1 revision files up and down with
``migrate``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
A0 = "billing_a0_renewals"
HEAD = "billing_p1_ledger_owner_check"
CHAIN = [
    "billing_p1_series_shadow",
    "billing_p1_invoice_columns",
    "billing_p1_adjustments",
    "billing_p1_refunds",
    "billing_p1_ledger_accounts",
    "billing_p1_cycles",
    "billing_p1_backfill_accounts",
    "billing_p1_backfill_payments",
    "billing_p1_invoice_checks",
    "billing_p1_ledger_owner_check",
]
NEW_TABLES = {
    "document_series",
    "billing_shadow_mismatches",
    "invoice_adjustments",
    "invoice_adjustment_lines",
    "refunds",
}


def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def script() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config())


def before(revision_id: str) -> str:
    index = CHAIN.index(revision_id)
    return A0 if index == 0 else CHAIN[index - 1]


def current(engine: sa.Engine) -> Optional[str]:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def migrate(engine: sa.Engine, target: str) -> None:
    """Upgrade or downgrade ``engine`` to ``target`` through the real revision files."""
    directory = script()
    start = current(engine)
    revisions = [A0, *CHAIN]
    upward = start is None or revisions.index(target) >= revisions.index(start)

    def steps(rev, _context):
        if upward:
            return directory._upgrade_revs(target, rev)
        return directory._downgrade_revs(target, rev)

    with EnvironmentContext(alembic_config(), directory, fn=steps, destination_rev=target) as env:
        with engine.connect() as conn:
            env.configure(connection=conn, transaction_per_migration=True)
            with env.begin_transaction():
                env.run_migrations()
            conn.commit()


def build_a0_database(engine: sa.Engine) -> None:
    """Create the schema at ``billing_a0_renewals`` (see the module docstring)."""
    ddl = (Path(__file__).parent / "billing_a0_schema.sql").read_text()
    raw = engine.raw_connection()
    try:
        raw.driver_connection.executescript(ddl)
        raw.commit()
    finally:
        raw.close()
    with engine.begin() as conn:
        MigrationContext.configure(conn).stamp(script(), A0)


def sqlite_table_sql(engine: sa.Engine, table: str) -> str:
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": table},
        ).scalar() or ""


def sqlite_indexes(engine: sa.Engine, table: str) -> set[tuple]:
    """Named indexes of ``table`` as (name, unique, columns); autoindexes by columns only."""
    found = set()
    with engine.connect() as conn:
        for row in conn.execute(sa.text(f"PRAGMA index_list('{table}')")).mappings():
            columns = tuple(
                info["name"]
                for info in conn.execute(sa.text(f"PRAGMA index_info('{row['name']}')")).mappings()
            )
            name = "" if row["name"].startswith("sqlite_autoindex") else row["name"]
            found.add((name, bool(row["unique"]), columns))
    return found


def sqlite_foreign_keys(engine: sa.Engine, table: str) -> set[tuple]:
    with engine.connect() as conn:
        return {
            (row["table"], row["from"], row["to"], row["on_delete"])
            for row in conn.execute(sa.text(f"PRAGMA foreign_key_list('{table}')")).mappings()
        }


def columns(engine: sa.Engine, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(engine).get_columns(table)}


def tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def execute(engine: sa.Engine, sql: str, **params):
    with engine.begin() as conn:
        return conn.execute(sa.text(sql), params)


def scalar(engine: sa.Engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar()
