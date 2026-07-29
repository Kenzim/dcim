"""Add reseller invoice sequencing and Stripe payment plumbing.

Revision ID: reseller_platform_phase4
Revises: reseller_platform_phase2
Create Date: 2026-07-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "reseller_platform_phase4"
down_revision: Union[str, None] = "reseller_platform_phase2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "resellers",
        sa.Column("stripe_customer_ref", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_resellers_stripe_customer_ref",
        "resellers",
        ["stripe_customer_ref"],
        unique=True,
    )

    op.create_table(
        "invoice_sequences",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("next_value", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    op.execute(
        sa.text(
            "INSERT INTO invoice_sequences (name, next_value) "
            "SELECT 'invoice', COALESCE(MAX(invoice_number), 100000) + 1 "
            "FROM invoices"
        )
    )

    op.add_column(
        "payments",
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reseller_payment_methods",
        sa.Column("brand", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reseller_payment_methods",
        sa.Column("last4", sa.String(length=4), nullable=True),
    )

    op.create_table(
        "gateway_webhook_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gateway", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=255), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "gateway", "event_id", name="uq_gateway_webhook_event"
        ),
    )
    op.create_index(
        "ix_gateway_webhook_events_id",
        "gateway_webhook_events",
        ["id"],
    )
    op.create_index(
        "ix_gateway_webhook_created",
        "gateway_webhook_events",
        ["gateway", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("gateway_webhook_events")
    op.drop_column("reseller_payment_methods", "last4")
    op.drop_column("reseller_payment_methods", "brand")
    op.drop_column("payments", "refunded_at")
    op.drop_table("invoice_sequences")
    op.drop_index("ix_resellers_stripe_customer_ref", table_name="resellers")
    op.drop_column("resellers", "stripe_customer_ref")
