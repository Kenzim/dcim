"""Billing phase 2: tax columns on existing tables

Adds the columns of docs/billing-phase2-design.md 1.7 on ``invoices``,
``orders``, ``billing_profiles``, ``billing_accounts``, ``products`` and
``price_plan_cycles``, plus ``billing_accounts.billing_late_fee_exempt``
(design 10.3). Every column is nullable or has a constant server default, so
on MariaDB 11 each ADD COLUMN is instant and no existing row is backfilled.
Foreign keys on the new columns are added with ``foreign_key_checks = 0`` on
MySQL (the columns are all NULL, so no violation is possible), as in
``billing_p1_invoice_columns``. On SQLite the columns are plain, as SQLite
cannot add a foreign key to an existing table.

Revision ID: billing_p2_tax_columns
Revises: billing_p2_tax_schema
Create Date: 2026-11-22 12:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p2_tax_columns"
down_revision: Union[str, Sequence[str], None] = "billing_p2_tax_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def columns() -> dict[str, list[sa.Column]]:
    return {
        "invoices": [
            sa.Column("tax_engine", sa.String(128), nullable=True),
            sa.Column("tax_point_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("tax_summary", sa.JSON(), nullable=True),
            sa.Column("tax_evidence", sa.JSON(), nullable=True),
            sa.Column("legal_notes", sa.JSON(), nullable=True),
            sa.Column("seller_registration_id", sa.Integer(), nullable=True),
        ],
        "orders": [
            sa.Column("tax_evidence", sa.JSON(), nullable=True),
        ],
        "billing_profiles": [
            sa.Column("tax_id_type", sa.String(16), nullable=True),
            sa.Column("tax_id_status", sa.String(16), nullable=False, server_default="unchecked"),
            sa.Column("tax_id_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "tax_id_check_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
            ),
            sa.Column("is_business", sa.Boolean(), nullable=False, server_default=sa.false()),
        ],
        "billing_accounts": [
            sa.Column("tax_exempt_reason", sa.String(255), nullable=True),
            sa.Column("billing_late_fee_exempt", sa.Boolean(), nullable=False, server_default=sa.false()),
        ],
        "products": [
            sa.Column("tax_class_id", sa.Integer(), nullable=True),
        ],
        "price_plan_cycles": [
            sa.Column("invoice_lead_days", sa.SmallInteger(), nullable=True),
        ],
    }


# (index name, table, column)
INDEXES = (
    ("ix_invoices_seller_registration_id", "invoices", "seller_registration_id"),
    ("ix_billing_profiles_tax_id_check_id", "billing_profiles", "tax_id_check_id"),
    ("ix_products_tax_class_id", "products", "tax_class_id"),
)
# (name, table, column, referent, ondelete)
FOREIGN_KEYS = (
    (
        "fk_invoices_seller_registration_id",
        "invoices",
        "seller_registration_id",
        "seller_tax_registrations",
        "RESTRICT",
    ),
    ("fk_billing_profiles_tax_id_check_id", "billing_profiles", "tax_id_check_id", "tax_id_checks", "SET NULL"),
    ("fk_products_tax_class_id", "products", "tax_class_id", "tax_classes", "RESTRICT"),
)


def _sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    for table, added in columns().items():
        for column in added:
            op.add_column(table, column)
    for name, table, column in INDEXES:
        op.create_index(name, table, [column])

    if not _sqlite():
        bind = op.get_bind()
        bind.execute(sa.text("SET foreign_key_checks = 0"))
        try:
            for name, table, column, referent, ondelete in FOREIGN_KEYS:
                op.create_foreign_key(name, table, referent, [column], ["id"], ondelete=ondelete)
        finally:
            bind.execute(sa.text("SET foreign_key_checks = 1"))


def downgrade() -> None:
    if not _sqlite():
        for name, table, _column, _referent, _ondelete in FOREIGN_KEYS:
            op.drop_constraint(name, table, type_="foreignkey")
    for name, table, _column in INDEXES:
        op.drop_index(name, table_name=table)
    # On SQLite (3.35 or later) the columns are plain once the indexes are gone,
    # so DROP COLUMN works in place.
    for table, added in reversed(list(columns().items())):
        for column in reversed(added):
            op.drop_column(table, column.name)
