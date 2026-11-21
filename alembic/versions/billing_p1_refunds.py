"""Billing phase 1: refunds table and refund columns on payments

Adds ``payments.refunded_cents`` (NOT NULL, server default 0, instant on
MariaDB 10.3+ and MySQL 8), ``fee_cents`` and ``net_cents``, and creates
``refunds`` (docs/billing-phase1-design.md 1.6 and 1.7).

Revision ID: billing_p1_refunds
Revises: billing_p1_adjustments
Create Date: 2026-11-21 12:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p1_refunds"
down_revision: Union[str, Sequence[str], None] = "billing_p1_adjustments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PAYMENT_COLUMNS = ("refunded_cents", "fee_cents", "net_cents")
REFUND_INDEXES = (
    ("ix_refunds_payment_id", ["payment_id"]),
    ("ix_refunds_invoice_id", ["invoice_id"]),
    ("ix_refunds_billing_account_id", ["billing_account_id"]),
    ("ix_refunds_reseller_id", ["reseller_id"]),
    ("ix_refunds_status_next_attempt", ["status", "next_attempt_at"]),
)


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column("refunded_cents", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("payments", sa.Column("fee_cents", sa.BigInteger(), nullable=True))
    op.add_column("payments", sa.Column("net_cents", sa.BigInteger(), nullable=True))

    op.create_table(
        "refunds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "payment_id", sa.Integer(), sa.ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "invoice_id", sa.Integer(), sa.ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "adjustment_id",
            sa.Integer(),
            sa.ForeignKey("invoice_adjustments.id", ondelete="RESTRICT"),
            nullable=True,
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
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("destination", sa.String(32), nullable=False),
        sa.Column("gateway", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("reason_text", sa.String(512), nullable=True),
        sa.Column("issue_credit_note", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "ledger_entry_id",
            sa.Integer(),
            sa.ForeignKey("credit_ledger_entries.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("gateway_refund_ref", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(128), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column(
            "requested_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("amount_cents > 0", name="ck_refunds_positive_amount"),
        sa.CheckConstraint(
            "billing_account_id IS NOT NULL OR reseller_id IS NOT NULL",
            name="ck_refunds_owner",
        ),
        sa.UniqueConstraint("gateway", "gateway_refund_ref", name="uq_refunds_gateway_ref"),
        sa.UniqueConstraint("adjustment_id", name="uq_refunds_adjustment_id"),
        sa.UniqueConstraint("ledger_entry_id", name="uq_refunds_ledger_entry_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_refunds_idempotency_key"),
    )
    for name, columns in REFUND_INDEXES:
        op.create_index(name, "refunds", columns)


def downgrade() -> None:
    # Dropping the table drops its indexes; MySQL refuses to drop an index a
    # foreign key still needs.
    op.drop_table("refunds")
    for name in reversed(PAYMENT_COLUMNS):
        op.drop_column("payments", name)
