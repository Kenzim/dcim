"""Billing phase 2: references to the frozen exchange rate

Adds ``invoices.fx_rate_id``, the ``tax_exchange_rates`` row an invoice froze
its sterling VAT from (docs/billing-phase2-design.md 4.10), with a foreign
key ``ON DELETE RESTRICT``, so the database itself refuses to delete a rate
an invoice uses whatever the deleting transaction's snapshot. Invoices that
froze a rate before this revision get the id of the row they froze (same
source, currency and month).

The column is nullable, so on MariaDB 11 the ADD COLUMN is instant. The
foreign key is added with ``foreign_key_checks = 0`` on MySQL, as in
``billing_p2_tax_columns``; on SQLite the column is plain, as SQLite cannot
add a foreign key to an existing table.

Revision ID: billing_p2_fx_refs
Revises: billing_p2_exchange_rates
Create Date: 2026-11-22 23:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p2_fx_refs"
down_revision: Union[str, Sequence[str], None] = "billing_p2_exchange_rates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def columns() -> dict[str, list[sa.Column]]:
    return {
        "invoices": [
            sa.Column("fx_rate_id", sa.Integer(), nullable=True),
        ],
    }


# (index name, table, column)
INDEXES = (("ix_invoices_fx_rate_id", "invoices", "fx_rate_id"),)
# (name, table, column)
FOREIGN_KEYS = (("fk_invoices_fx_rate_id", "invoices", "fx_rate_id"),)


def _sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    for table, added in columns().items():
        for column in added:
            op.add_column(table, column)
    for name, table, column in INDEXES:
        op.create_index(name, table, [column])
    op.execute(
        sa.text(
            "UPDATE invoices SET fx_rate_id = (SELECT r.id FROM tax_exchange_rates r "
            "WHERE r.source = invoices.fx_source AND r.base = 'GBP' AND r.currency = invoices.currency "
            "AND r.period = invoices.fx_period) WHERE fx_rate IS NOT NULL"
        )
    )
    if not _sqlite():
        bind = op.get_bind()
        bind.execute(sa.text("SET foreign_key_checks = 0"))
        try:
            for name, table, column in FOREIGN_KEYS:
                op.create_foreign_key(name, table, "tax_exchange_rates", [column], ["id"], ondelete="RESTRICT")
        finally:
            bind.execute(sa.text("SET foreign_key_checks = 1"))


def downgrade() -> None:
    if not _sqlite():
        for name, table, _column in FOREIGN_KEYS:
            op.drop_constraint(name, table, type_="foreignkey")
    for name, table, _column in INDEXES:
        op.drop_index(name, table_name=table)
    # On SQLite (3.35 or later) the columns are plain once the indexes are gone,
    # so DROP COLUMN works in place.
    for table, added in reversed(list(columns().items())):
        for column in reversed(added):
            op.drop_column(table, column.name)
