"""Billing phase 1: invoice checks for model v2

Drops ``ck_invoices_positive_amount`` (``amount_cents > 0``) and adds
``ck_invoices_amount_nonnegative``, ``ck_invoices_v2_totals`` and
``ck_invoices_v2_number`` (docs/billing-phase1-design.md 1.2 and 2.4).

LOCKS: on MySQL/MariaDB adding a CHECK validates every row and rebuilds
``invoices`` with writes blocked (ALGORITHM=COPY). The drop and the three adds
are one ALTER TABLE so the table is rebuilt once. The constraint is looked up
in information_schema first, because MySQL before 8.0.16 ignored CHECKs and a
very old database may not have it. If the rehearsal shows this step over
budget, run ``alembic upgrade billing_p1_backfill_payments`` in the normal
deploy and this revision later in a window.

SQLite cannot drop a constraint, so the table is recreated from an explicit
definition of ``invoices`` as it stands before this revision, with foreign key
enforcement off around the rebuild.

Revision ID: billing_p1_invoice_checks
Revises: billing_p1_backfill_payments
Create Date: 2026-11-21 13:20:00.000000
"""

from contextlib import contextmanager
from typing import Iterator, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p1_invoice_checks"
down_revision: Union[str, Sequence[str], None] = "billing_p1_backfill_payments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "invoices"
OLD_CHECK = ("ck_invoices_positive_amount", "amount_cents > 0")
NEW_CHECKS = (
    ("ck_invoices_amount_nonnegative", "amount_cents >= 0"),
    (
        "ck_invoices_v2_totals",
        "model_version IS NULL OR (subtotal_cents IS NOT NULL AND discount_cents IS NOT NULL "
        "AND tax_cents IS NOT NULL AND prices_include_tax IS NOT NULL)",
    ),
    (
        "ck_invoices_v2_number",
        "model_version IS NULL OR status = 'draft' OR number_display IS NOT NULL "
        "OR proforma_number_display IS NOT NULL",
    ),
)


def invoices_table(*, v2_checks: bool) -> sa.Table:
    """``invoices`` as SQLite holds it after ``billing_p1_invoice_columns``.

    ``v2_checks`` False is the state before this revision (the old positive
    amount check), True the state after it.
    """
    metadata = sa.MetaData()
    checks = NEW_CHECKS if v2_checks else (OLD_CHECK,)
    table = sa.Table(
        TABLE,
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_number", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("reseller_id", sa.Integer(), sa.ForeignKey("resellers.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "billing_account_id",
            sa.Integer(),
            sa.ForeignKey("billing_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("model_version", sa.SmallInteger(), nullable=True),
        sa.Column("document_type", sa.String(32), nullable=False, server_default="invoice"),
        sa.Column("subtotal_cents", sa.BigInteger(), nullable=True),
        sa.Column("discount_cents", sa.BigInteger(), nullable=True),
        sa.Column("tax_cents", sa.BigInteger(), nullable=True),
        sa.Column("prices_include_tax", sa.Boolean(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seller_snapshot", sa.JSON(), nullable=True),
        sa.Column("buyer_snapshot", sa.JSON(), nullable=True),
        sa.Column("customer_notes", sa.Text(), nullable=True),
        sa.Column("admin_notes", sa.Text(), nullable=True),
        sa.Column("locale", sa.String(16), nullable=True),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("number_period", sa.String(8), nullable=True),
        sa.Column("number_value", sa.BigInteger(), nullable=True),
        sa.Column("number_display", sa.String(64), nullable=True),
        sa.Column("proforma_series_id", sa.Integer(), nullable=True),
        sa.Column("proforma_number_period", sa.String(8), nullable=True),
        sa.Column("proforma_number_value", sa.BigInteger(), nullable=True),
        sa.Column("proforma_number_display", sa.String(64), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("void_reason", sa.String(255), nullable=True),
        *(sa.CheckConstraint(sql, name=name) for name, sql in checks),
    )
    for column in ("id", "reseller_id", "service_id", "billing_account_id", "order_id", "purpose", "status"):
        sa.Index(f"ix_invoices_{column}", table.c[column])
    sa.Index("ix_invoices_reseller_status_due", table.c.reseller_id, table.c.status, table.c.due_at)
    sa.Index("ix_invoices_series_id", table.c.series_id)
    sa.Index(
        "ix_invoices_account_status_due", table.c.billing_account_id, table.c.status, table.c.due_at
    )
    sa.Index(
        "uq_invoices_series_number",
        table.c.series_id,
        table.c.number_period,
        table.c.number_value,
        unique=True,
    )
    sa.Index("uq_invoices_series_display", table.c.series_id, table.c.number_display, unique=True)
    sa.Index(
        "uq_invoices_proforma_number",
        table.c.proforma_series_id,
        table.c.proforma_number_period,
        table.c.proforma_number_value,
        unique=True,
    )
    return table


@contextmanager
def sqlite_foreign_keys_off() -> Iterator[None]:
    """Turn SQLite foreign key enforcement off around a table rebuild.

    The pragma is ignored inside a transaction, so it is issued in an
    autocommit block; it is restored only if it was on.
    """
    context = op.get_context()
    with context.autocommit_block():
        bind = op.get_bind()
        was_on = bool(bind.execute(sa.text("PRAGMA foreign_keys")).scalar())
        bind.execute(sa.text("PRAGMA foreign_keys=OFF"))
    try:
        yield
    finally:
        if was_on:
            with context.autocommit_block():
                op.get_bind().execute(sa.text("PRAGMA foreign_keys=ON"))


def mysql_check_names(table: str) -> set[str]:
    rows = op.get_bind().execute(
        sa.text(
            "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table AND CONSTRAINT_TYPE = 'CHECK'"
        ),
        {"table": table},
    )
    return {str(name) for (name,) in rows}


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with sqlite_foreign_keys_off():
            with op.batch_alter_table(
                TABLE, recreate="always", copy_from=invoices_table(v2_checks=False)
            ) as batch:
                batch.drop_constraint(OLD_CHECK[0], type_="check")
                for name, sql in NEW_CHECKS:
                    batch.create_check_constraint(name, sql)
        return

    present = mysql_check_names(TABLE)
    clauses = [f"DROP CONSTRAINT {OLD_CHECK[0]}"] if OLD_CHECK[0] in present else []
    clauses += [f"ADD CONSTRAINT {name} CHECK ({sql})" for name, sql in NEW_CHECKS if name not in present]
    if clauses:
        op.execute(f"ALTER TABLE {TABLE} " + ", ".join(clauses))


def _refuse_if_zero_amount_invoices() -> None:
    count = op.get_bind().execute(
        sa.text(f"SELECT COUNT(*) FROM {TABLE} WHERE amount_cents <= 0")
    ).scalar()
    if count:
        raise RuntimeError(
            f"cannot downgrade {revision}: {count} invoices have amount_cents <= 0, which the "
            f"restored check {OLD_CHECK[0]} ({OLD_CHECK[1]}) would reject; void or correct them first"
        )


def downgrade() -> None:
    _refuse_if_zero_amount_invoices()
    if op.get_bind().dialect.name == "sqlite":
        with sqlite_foreign_keys_off():
            with op.batch_alter_table(
                TABLE, recreate="always", copy_from=invoices_table(v2_checks=True)
            ) as batch:
                for name, _sql in NEW_CHECKS:
                    batch.drop_constraint(name, type_="check")
                batch.create_check_constraint(OLD_CHECK[0], OLD_CHECK[1])
        return

    present = mysql_check_names(TABLE)
    clauses = [f"DROP CONSTRAINT {name}" for name, _sql in NEW_CHECKS if name in present]
    if OLD_CHECK[0] not in present:
        clauses.append(f"ADD CONSTRAINT {OLD_CHECK[0]} CHECK ({OLD_CHECK[1]})")
    if clauses:
        op.execute(f"ALTER TABLE {TABLE} " + ", ".join(clauses))
