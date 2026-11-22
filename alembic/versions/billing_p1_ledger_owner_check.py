"""Billing phase 1: owner check on credit ledger entries

Adds ``ck_credit_ledger_owner``: ``reseller_id IS NOT NULL OR
billing_account_id IS NOT NULL`` (docs/billing-phase1-design.md 1.8 and 2.4).
Every existing row has ``reseller_id``, so validation cannot fail.

LOCKS: on MySQL/MariaDB adding the CHECK rebuilds ``credit_ledger_entries``
with writes blocked (ALGORITHM=COPY); this is the largest money table on a
busy reseller install, which is why this revision is last. Until it runs,
``CreditLedgerService`` enforces the rule in code and the E2 detector checks
it. On SQLite the table is recreated from an explicit definition, as in
``billing_p1_ledger_accounts``.

Revision ID: billing_p1_ledger_owner_check
Revises: billing_p1_invoice_checks
Create Date: 2026-11-21 13:30:00.000000
"""

from contextlib import contextmanager
from typing import Iterator, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p1_ledger_owner_check"
down_revision: Union[str, Sequence[str], None] = "billing_p1_invoice_checks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "credit_ledger_entries"
CHECK_NAME = "ck_credit_ledger_owner"
CHECK_SQL = "reseller_id IS NOT NULL OR billing_account_id IS NOT NULL"


def ledger_table(*, owner_check: bool) -> sa.Table:
    """``credit_ledger_entries`` as SQLite holds it after ``billing_p1_ledger_accounts``."""
    metadata = sa.MetaData()
    constraints = [sa.CheckConstraint("amount_cents <> 0", name="ck_credit_ledger_nonzero_amount")]
    if owner_check:
        constraints.append(sa.CheckConstraint(CHECK_SQL, name=CHECK_NAME))
    table = sa.Table(
        TABLE,
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reseller_id", sa.Integer(), sa.ForeignKey("resellers.id", ondelete="RESTRICT"), nullable=True),
        sa.Column(
            "billing_account_id",
            sa.Integer(),
            sa.ForeignKey(
                "billing_accounts.id", ondelete="RESTRICT", name="fk_credit_ledger_billing_account_id"
            ),
            nullable=True,
        ),
        sa.Column("entry_type", sa.String(32), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("balance_after_cents", sa.BigInteger(), nullable=False),
        sa.Column("description", sa.String(512), nullable=True),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "reversal_of_id",
            sa.Integer(),
            sa.ForeignKey("credit_ledger_entries.id", ondelete="RESTRICT"),
            nullable=True,
            unique=True,
        ),
        sa.Column("reference_type", sa.String(64), nullable=True),
        sa.Column("reference_id", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=True, unique=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        *constraints,
    )
    for column in (
        "id",
        "reseller_id",
        "entry_type",
        "invoice_id",
        "payment_id",
        "service_id",
        "created_by_user_id",
        "created_at",
    ):
        sa.Index(f"ix_credit_ledger_entries_{column}", table.c[column])
    sa.Index("ix_credit_ledger_reseller_created", table.c.reseller_id, table.c.created_at)
    sa.Index("ix_credit_ledger_account_id", table.c.billing_account_id, table.c.id)
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


def _mysql_has_check() -> bool:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = :table AND CONSTRAINT_TYPE = 'CHECK' AND CONSTRAINT_NAME = :name"
            ),
            {"table": TABLE, "name": CHECK_NAME},
        )
        .first()
        is not None
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with sqlite_foreign_keys_off():
            with op.batch_alter_table(
                TABLE, recreate="always", copy_from=ledger_table(owner_check=False)
            ) as batch:
                batch.create_check_constraint(CHECK_NAME, CHECK_SQL)
        return
    if not _mysql_has_check():
        op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK_NAME} CHECK ({CHECK_SQL})")


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with sqlite_foreign_keys_off():
            with op.batch_alter_table(
                TABLE, recreate="always", copy_from=ledger_table(owner_check=True)
            ) as batch:
                batch.drop_constraint(CHECK_NAME, type_="check")
        return
    if _mysql_has_check():
        op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {CHECK_NAME}")
