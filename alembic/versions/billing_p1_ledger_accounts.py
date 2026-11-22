"""Billing phase 1: billing account on credit ledger entries

``credit_ledger_entries`` gains ``billing_account_id`` (nullable; NOT NULL is a
later contract step), ``reseller_id`` becomes nullable so retail entries can
exist, plus index ``ix_credit_ledger_account_id`` and foreign key
``fk_credit_ledger_billing_account_id``. docs/billing-phase1-design.md 1.8, 2.4.

MySQL/MariaDB: ADD COLUMN is instant; ``MODIFY reseller_id INT NULL`` is an
in-place rebuild that allows concurrent reads and writes; the index is built
online; the foreign key is added with ``foreign_key_checks = 0`` (the column is
all NULL), which MariaDB reports as NO ACTION rather than RESTRICT (InnoDB
enforces the two identically). SQLite cannot alter a column, so the table is recreated from an
explicit copy of its current definition.

Revision ID: billing_p1_ledger_accounts
Revises: billing_p1_refunds
Create Date: 2026-11-21 12:40:00.000000
"""

from contextlib import contextmanager
from typing import Iterator, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p1_ledger_accounts"
down_revision: Union[str, Sequence[str], None] = "billing_p1_refunds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "credit_ledger_entries"
FK_NAME = "fk_credit_ledger_billing_account_id"
INDEX_NAME = "ix_credit_ledger_account_id"


def ledger_table(*, with_account: bool, owner_check: bool) -> sa.Table:
    """``credit_ledger_entries`` as SQLite holds it, before or after this chain's changes."""
    metadata = sa.MetaData()
    columns = [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reseller_id",
            sa.Integer(),
            sa.ForeignKey("resellers.id", ondelete="RESTRICT"),
            nullable=with_account,
        ),
    ]
    if with_account:
        columns.append(
            sa.Column(
                "billing_account_id",
                sa.Integer(),
                sa.ForeignKey("billing_accounts.id", ondelete="RESTRICT", name=FK_NAME),
                nullable=True,
            )
        )
    columns += [
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
    ]
    constraints = [sa.CheckConstraint("amount_cents <> 0", name="ck_credit_ledger_nonzero_amount")]
    if owner_check:
        constraints.append(
            sa.CheckConstraint(
                "reseller_id IS NOT NULL OR billing_account_id IS NOT NULL",
                name="ck_credit_ledger_owner",
            )
        )
    table = sa.Table(TABLE, metadata, *columns, *constraints)
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
    if with_account:
        sa.Index(INDEX_NAME, table.c.billing_account_id, table.c.id)
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


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with sqlite_foreign_keys_off():
            with op.batch_alter_table(
                TABLE,
                recreate="always",
                copy_from=ledger_table(with_account=False, owner_check=False),
            ) as batch:
                batch.add_column(sa.Column("billing_account_id", sa.Integer(), nullable=True))
                batch.create_foreign_key(
                    FK_NAME, "billing_accounts", ["billing_account_id"], ["id"], ondelete="RESTRICT"
                )
                batch.alter_column("reseller_id", existing_type=sa.Integer(), nullable=True)
                batch.create_index(INDEX_NAME, ["billing_account_id", "id"])
        return

    op.add_column(TABLE, sa.Column("billing_account_id", sa.Integer(), nullable=True))
    op.alter_column(TABLE, "reseller_id", existing_type=sa.Integer(), nullable=True)
    op.create_index(INDEX_NAME, TABLE, ["billing_account_id", "id"])
    bind = op.get_bind()
    bind.execute(sa.text("SET foreign_key_checks = 0"))
    try:
        op.create_foreign_key(
            FK_NAME, TABLE, "billing_accounts", ["billing_account_id"], ["id"], ondelete="RESTRICT"
        )
    finally:
        bind.execute(sa.text("SET foreign_key_checks = 1"))


def _refuse_if_retail_entries() -> None:
    count = op.get_bind().execute(
        sa.text(f"SELECT COUNT(*) FROM {TABLE} WHERE reseller_id IS NULL")
    ).scalar()
    if count:
        raise RuntimeError(
            f"cannot downgrade {revision}: {count} credit ledger entries have no reseller "
            "(retail entries); reseller_id cannot become NOT NULL again"
        )


def downgrade() -> None:
    _refuse_if_retail_entries()
    if op.get_bind().dialect.name == "sqlite":
        with sqlite_foreign_keys_off():
            with op.batch_alter_table(
                TABLE,
                recreate="always",
                copy_from=ledger_table(with_account=True, owner_check=False),
            ) as batch:
                batch.drop_index(INDEX_NAME)
                batch.drop_constraint(FK_NAME, type_="foreignkey")
                batch.drop_column("billing_account_id")
                batch.alter_column("reseller_id", existing_type=sa.Integer(), nullable=False)
        return

    op.drop_constraint(FK_NAME, TABLE, type_="foreignkey")
    op.drop_index(INDEX_NAME, table_name=TABLE)
    op.alter_column(TABLE, "reseller_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column(TABLE, "billing_account_id")
