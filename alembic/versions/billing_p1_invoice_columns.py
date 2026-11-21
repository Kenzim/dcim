"""Billing phase 1: invoice model v2 columns on invoices and invoice_lines

Adds every column of docs/billing-phase1-design.md 1.2 and 1.3. All columns are
nullable or have a constant server default, so on MariaDB 11 each ADD COLUMN is
instant; index builds are online. Foreign keys on the new columns are added
with ``foreign_key_checks = 0`` on MySQL (the columns are all NULL, so no
violation is possible). MariaDB then reports the ON DELETE RESTRICT rule as
NO ACTION; InnoDB enforces the two identically. On SQLite the columns are
plain, as SQLite cannot add a foreign key to an existing table.

Revision ID: billing_p1_invoice_columns
Revises: billing_p1_series_shadow
Create Date: 2026-11-21 12:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p1_invoice_columns"
down_revision: Union[str, Sequence[str], None] = "billing_p1_series_shadow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def invoice_columns() -> list[sa.Column]:
    return [
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
    ]


def line_columns() -> list[sa.Column]:
    return [
        sa.Column("kind", sa.String(32), nullable=True),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("service_billing_id", sa.Integer(), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tax_class_id", sa.Integer(), nullable=True),
        sa.Column("taxable", sa.Boolean(), nullable=True),
        sa.Column("tax_snapshot", sa.JSON(), nullable=True),
    ]


# (name, table, columns)
INVOICE_UNIQUES = (
    ("uq_invoices_series_number", ["series_id", "number_period", "number_value"]),
    ("uq_invoices_series_display", ["series_id", "number_display"]),
    (
        "uq_invoices_proforma_number",
        ["proforma_series_id", "proforma_number_period", "proforma_number_value"],
    ),
)
INVOICE_INDEXES = (
    ("ix_invoices_series_id", ["series_id"]),
    ("ix_invoices_account_status_due", ["billing_account_id", "status", "due_at"]),
)
LINE_INDEXES = (
    ("ix_invoice_lines_service_id", ["service_id"]),
    ("ix_invoice_lines_service_billing_id", ["service_billing_id"]),
)
# (name, table, column, referent, ondelete)
FOREIGN_KEYS = (
    ("fk_invoices_series_id", "invoices", "series_id", "document_series", "RESTRICT"),
    ("fk_invoices_proforma_series_id", "invoices", "proforma_series_id", "document_series", "RESTRICT"),
    ("fk_invoice_lines_service_id", "invoice_lines", "service_id", "services", "SET NULL"),
    (
        "fk_invoice_lines_service_billing_id",
        "invoice_lines",
        "service_billing_id",
        "service_billings",
        "SET NULL",
    ),
)


def _sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    sqlite = _sqlite()
    for column in invoice_columns():
        op.add_column("invoices", column)
    for column in line_columns():
        op.add_column("invoice_lines", column)

    for name, columns in INVOICE_UNIQUES:
        if sqlite:
            # SQLite cannot ALTER a constraint in; a unique index is equivalent.
            op.create_index(name, "invoices", columns, unique=True)
        else:
            op.create_unique_constraint(name, "invoices", columns)
    for name, columns in INVOICE_INDEXES:
        op.create_index(name, "invoices", columns)
    for name, columns in LINE_INDEXES:
        op.create_index(name, "invoice_lines", columns)

    if not sqlite:
        bind = op.get_bind()
        bind.execute(sa.text("SET foreign_key_checks = 0"))
        try:
            for name, table, column, referent, ondelete in FOREIGN_KEYS:
                op.create_foreign_key(name, table, referent, [column], ["id"], ondelete=ondelete)
        finally:
            bind.execute(sa.text("SET foreign_key_checks = 1"))


def downgrade() -> None:
    sqlite = _sqlite()
    if not sqlite:
        for name, table, _column, _referent, _ondelete in FOREIGN_KEYS:
            op.drop_constraint(name, table, type_="foreignkey")
    for name, _columns in LINE_INDEXES:
        op.drop_index(name, table_name="invoice_lines")
    for name, _columns in INVOICE_INDEXES:
        op.drop_index(name, table_name="invoices")
    for name, _columns in INVOICE_UNIQUES:
        if sqlite:
            op.drop_index(name, table_name="invoices")
        else:
            op.drop_constraint(name, "invoices", type_="unique")

    # On SQLite (3.35 or later) the columns are plain once the indexes are gone,
    # so DROP COLUMN works in place.
    for column in line_columns():
        op.drop_column("invoice_lines", column.name)
    for column in invoice_columns():
        op.drop_column("invoices", column.name)
