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
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
A0 = "billing_a0_renewals"
P1_HEAD = "billing_p1_ledger_owner_check"
# The single alembic head; the phase 2 chain follows the phase 1 head.
HEAD = "billing_p2_email_templates"
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
# docs/billing-phase2-design.md 14.4.
P2_CHAIN = [
    "plugin_p2_effective_api",
    "billing_p2_tax_schema",
    "billing_p2_tax_columns",
    "billing_p2_tax_seed",
    "billing_p2_automation",
    "billing_p2_email_templates",
]
P2_NEW_TABLES = {
    "tax_classes",
    "tax_rules",
    "invoice_profiles",
    "seller_tax_registrations",
    "tax_exemptions",
    "tax_legal_notes",
    "tax_id_checks",
    "invoice_line_taxes",
    "invoice_adjustment_line_taxes",
    "billing_job_runs",
    "billing_job_states",
    "billing_job_run_items",
    "billing_job_actions",
    "billing_nonpayment_policies",
    "cancellation_requests",
    "email_templates",
    "email_template_versions",
}
# Columns and indexes the phase 2 chain adds to existing tables.
P2_COLUMNS = {
    "plugin_installations": {"effective_api"},
    "invoices": {"tax_engine", "tax_point_at", "tax_summary", "tax_evidence", "legal_notes", "seller_registration_id"},
    "orders": {"tax_evidence"},
    "billing_profiles": {"tax_id_type", "tax_id_status", "tax_id_checked_at", "tax_id_check_id", "is_business"},
    "billing_accounts": {"tax_exempt_reason", "billing_late_fee_exempt"},
    "products": {"tax_class_id"},
    "price_plan_cycles": {"invoice_lead_days"},
    "email_messages": {"template_id", "template_version", "render_fallback"},
}
P2_INDEXES = {
    "ix_invoices_seller_registration_id",
    "ix_billing_profiles_tax_id_check_id",
    "ix_products_tax_class_id",
    "ix_email_messages_template_id",
}
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
    revisions = [A0, *CHAIN, *P2_CHAIN]
    return revisions[revisions.index(revision_id) - 1]


def current(engine: sa.Engine) -> Optional[str]:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def migrate(engine: sa.Engine, target: str) -> None:
    """Upgrade or downgrade ``engine`` to ``target`` through the real revision files.

    Runs ``alembic/env.py`` itself on a connection of ``engine``, so the tests
    use the transaction configuration of a real ``alembic upgrade``.
    """
    start = current(engine)
    revisions = [A0, *CHAIN, *P2_CHAIN]
    upward = start is None or start not in revisions or revisions.index(target) >= revisions.index(start)
    config = alembic_config()
    with engine.connect() as conn:
        config.attributes["connection"] = conn
        if upward:
            command.upgrade(config, target)
        else:
            command.downgrade(config, target)


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


def build_p1_head_database(engine: sa.Engine) -> None:
    """The a0 schema taken to the phase 1 head, plus the tables phase 2 touches.

    ``billing_p2_base_schema.sql`` holds the tables the phase 2 chain alters that
    ``billing_a0_schema.sql`` does not, as they stand at the phase 1 head.
    """
    build_a0_database(engine)
    migrate(engine, P1_HEAD)
    ddl = (Path(__file__).parent / "billing_p2_base_schema.sql").read_text()
    raw = engine.raw_connection()
    try:
        raw.driver_connection.executescript(ddl)
        raw.commit()
    finally:
        raw.close()


def seed_p2_data(engine: sa.Engine) -> None:
    """Rows present before the phase 2 chain (design 15.1).

    Two ``tax_rates`` rows (one with a region, one without and disabled), a client
    billing account with its billing profile, and an email message.
    """
    execute(engine, "INSERT INTO users (id, username, email, is_admin) VALUES (41, 'p2', 'p2@example.com', 0)")
    execute(
        engine,
        "INSERT INTO billing_accounts (id, account_type, user_id, status, currency, credit_balance_cents, "
        "tax_exempt) VALUES (41, 'client', 41, 'active', 'GBP', 0, 0)",
    )
    execute(
        engine,
        "INSERT INTO billing_profiles (id, billing_account_id, company, country, tax_id) "
        "VALUES (41, 41, 'Acme Ltd', 'GB', 'GB123456789')",
    )
    execute(
        engine,
        "INSERT INTO email_messages (id, to_address, subject, status, attempts, event, billing_account_id) "
        "VALUES (41, 'p2@example.com', 'Hello', 'sent', 1, 'invoice_paid', 41)",
    )
    execute(
        engine,
        "INSERT INTO tax_rates (id, country, region, rate_bps, name, enabled) VALUES "
        "(7, 'GB', NULL, 2000, 'UK VAT', 1), (8, 'US', 'CA', 725, 'California sales tax', 0)",
    )


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


def load_revision(revision_id: str):
    """Import one revision file as a module (to reach its constants)."""
    import importlib.util

    path = ROOT / "alembic" / "versions" / f"{revision_id}.py"
    spec = importlib.util.spec_from_file_location(revision_id, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
