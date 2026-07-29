"""Add recurring reseller billing, dunning, and notification outbox.

Revision ID: reseller_platform_phase7_recurring
Revises: reseller_platform_phase6_usdt
Create Date: 2026-07-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "reseller_platform_phase7_recurring"
down_revision: Union[str, None] = "reseller_platform_phase6_usdt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CYCLE_STATES = (
    "due",
    "processing",
    "past_due",
    "grace",
    "pending_action",
    "paid",
    "suspended",
    "cancelled",
    "manual_review",
    "error",
)
_OUTBOX_STATUSES = ("queued", "sending", "sent", "skipped", "failed")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.add_column(
        "resellers",
        sa.Column(
            "nonpayment_policy",
            sa.Enum("block_new", "suspend_all", native_enum=False),
            server_default="block_new",
            nullable=False,
        ),
    )
    op.add_column(
        "resellers",
        sa.Column(
            "billing_hold",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "resellers",
        sa.Column("billing_hold_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "resellers",
        sa.Column(
            "billing_hold_cleared_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "resellers",
        sa.Column("billing_hold_reason", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "service_billings",
        sa.Column("billing_anchor_day", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_service_billings_anchor_day",
        "service_billings",
        "billing_anchor_day IS NULL OR "
        "(billing_anchor_day >= 1 AND billing_anchor_day <= 31)",
    )

    op.create_table(
        "billing_cycles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("service_billing_id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default="USD",
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(*_CYCLE_STATES, native_enum=False),
            server_default="due",
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("claim_token", sa.String(length=128), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "amount_cents >= 0",
            name="ck_billing_cycles_nonnegative_amount",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_billing_cycles_attempts_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["service_billing_id"],
            ["service_billings.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_id"),
        sa.UniqueConstraint(
            "service_billing_id",
            "due_at",
            name="uq_billing_cycles_billing_due",
        ),
    )
    op.create_index("ix_billing_cycles_id", "billing_cycles", ["id"])
    op.create_index(
        "ix_billing_cycles_service_billing_id",
        "billing_cycles",
        ["service_billing_id"],
    )
    op.create_index(
        "ix_billing_cycles_invoice_id",
        "billing_cycles",
        ["invoice_id"],
    )
    op.create_index("ix_billing_cycles_due_at", "billing_cycles", ["due_at"])
    op.create_index("ix_billing_cycles_state", "billing_cycles", ["state"])
    op.create_index(
        "ix_billing_cycles_next_retry_at",
        "billing_cycles",
        ["next_retry_at"],
    )
    op.create_index(
        "ix_billing_cycles_state_retry",
        "billing_cycles",
        ["state", "next_retry_at"],
    )
    op.create_index(
        "ix_billing_cycles_claim",
        "billing_cycles",
        ["claim_expires_at", "claim_token"],
    )

    op.create_table(
        "recurring_billing_leases",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_index(
        "ix_recurring_billing_leases_owner",
        "recurring_billing_leases",
        ["owner"],
    )
    op.create_index(
        "ix_recurring_billing_leases_expires_at",
        "recurring_billing_leases",
        ["expires_at"],
    )

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=True),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("template", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_OUTBOX_STATUSES, native_enum=False),
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claim_token", sa.String(length=128), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_notification_outbox_attempts_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            ["resellers.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_notification_outbox_id", "notification_outbox", ["id"])
    op.create_index(
        "ix_notification_outbox_reseller_id",
        "notification_outbox",
        ["reseller_id"],
    )
    op.create_index(
        "ix_notification_outbox_recipient",
        "notification_outbox",
        ["recipient"],
    )
    op.create_index(
        "ix_notification_outbox_event",
        "notification_outbox",
        ["event"],
    )
    op.create_index(
        "ix_notification_outbox_status",
        "notification_outbox",
        ["status"],
    )
    op.create_index(
        "ix_notification_outbox_next_attempt_at",
        "notification_outbox",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_notification_outbox_status_attempt",
        "notification_outbox",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_notification_outbox_claim",
        "notification_outbox",
        ["claim_expires_at", "claim_token"],
    )


def downgrade() -> None:
    op.drop_table("notification_outbox")
    op.drop_table("recurring_billing_leases")
    op.drop_table("billing_cycles")
    op.drop_constraint(
        "ck_service_billings_anchor_day",
        "service_billings",
        type_="check",
    )
    op.drop_column("service_billings", "billing_anchor_day")
    op.drop_column("resellers", "billing_hold_reason")
    op.drop_column("resellers", "billing_hold_cleared_at")
    op.drop_column("resellers", "billing_hold_at")
    op.drop_column("resellers", "billing_hold")
    op.drop_column("resellers", "nonpayment_policy")
