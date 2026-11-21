"""Billing phase 1: credit and debit notes (invoice adjustments)

Creates ``invoice_adjustments`` and ``invoice_adjustment_lines``
(docs/billing-phase1-design.md 1.4 and 1.5). New tables only.

Revision ID: billing_p1_adjustments
Revises: billing_p1_invoice_columns
Create Date: 2026-11-21 12:20:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p1_adjustments"
down_revision: Union[str, Sequence[str], None] = "billing_p1_invoice_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "invoice_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "invoice_id", sa.Integer(), sa.ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "billing_account_id",
            sa.Integer(),
            sa.ForeignKey("billing_accounts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "reseller_id", sa.Integer(), sa.ForeignKey("resellers.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column(
            "series_id",
            sa.Integer(),
            sa.ForeignKey("document_series.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("number_period", sa.String(8), nullable=False, server_default=""),
        sa.Column("number_value", sa.BigInteger(), nullable=False),
        sa.Column("number_display", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("subtotal_cents", sa.BigInteger(), nullable=False),
        sa.Column("tax_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_cents", sa.BigInteger(), nullable=False),
        sa.Column("reason_code", sa.String(32), nullable=False),
        sa.Column("reason_text", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="issued"),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column(
            "ledger_entry_id",
            sa.Integer(),
            sa.ForeignKey("credit_ledger_entries.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seller_snapshot", sa.JSON(), nullable=True),
        sa.Column("buyer_snapshot", sa.JSON(), nullable=True),
        sa.Column("customer_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "billing_account_id IS NOT NULL OR reseller_id IS NOT NULL",
            name="ck_invoice_adjustments_owner",
        ),
        sa.CheckConstraint(
            "subtotal_cents >= 0 AND tax_cents >= 0 AND total_cents > 0 "
            "AND total_cents = subtotal_cents + tax_cents",
            name="ck_invoice_adjustments_amounts",
        ),
        sa.CheckConstraint(
            "(kind = 'credit' AND outcome IN ('reduced_balance','refunded','credited_to_balance')) "
            "OR (kind = 'debit' AND outcome = 'increased_balance') "
            "OR (kind = 'debit' AND outcome = 'credited_to_balance' AND reason_code = 'overpayment')",
            name="ck_invoice_adjustments_outcome",
        ),
        sa.UniqueConstraint(
            "series_id", "number_period", "number_value", name="uq_invoice_adjustments_number"
        ),
        sa.UniqueConstraint("series_id", "number_display", name="uq_invoice_adjustments_display"),
        sa.UniqueConstraint("ledger_entry_id", name="uq_invoice_adjustments_ledger_entry_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_invoice_adjustments_idempotency_key"),
    )
    op.create_index("ix_invoice_adjustments_invoice_id", "invoice_adjustments", ["invoice_id"])
    op.create_index(
        "ix_invoice_adjustments_billing_account_id", "invoice_adjustments", ["billing_account_id"]
    )
    op.create_index("ix_invoice_adjustments_reseller_id", "invoice_adjustments", ["reseller_id"])
    op.create_index(
        "ix_invoice_adjustments_invoice_kind", "invoice_adjustments", ["invoice_id", "kind"]
    )

    op.create_table(
        "invoice_adjustment_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "adjustment_id",
            sa.Integer(),
            sa.ForeignKey("invoice_adjustments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invoice_line_id",
            sa.Integer(),
            sa.ForeignKey("invoice_lines.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("description", sa.String(512), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_cents", sa.BigInteger(), nullable=False),
        sa.Column("total_cents", sa.BigInteger(), nullable=False),
        sa.Column("tax_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tax_class_id", sa.Integer(), nullable=True),
        sa.Column("tax_snapshot", sa.JSON(), nullable=True),
        sa.Column(
            "service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "quantity >= 1 AND unit_cents >= 0 AND total_cents >= 0 AND tax_cents >= 0",
            name="ck_invoice_adjustment_lines_amounts",
        ),
    )
    op.create_index(
        "ix_invoice_adjustment_lines_adjustment_id", "invoice_adjustment_lines", ["adjustment_id"]
    )


def downgrade() -> None:
    # Dropping a table drops its indexes; MySQL refuses to drop an index a
    # foreign key still needs.
    op.drop_table("invoice_adjustment_lines")
    op.drop_table("invoice_adjustments")
