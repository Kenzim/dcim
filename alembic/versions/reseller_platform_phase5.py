"""Add tenant-bound pending PayPal vault setups.

Revision ID: reseller_platform_phase5
Revises: reseller_platform_phase4
Create Date: 2026-07-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "reseller_platform_phase5"
down_revision: Union[str, None] = "reseller_platform_phase4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "paypal_pending_setups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=False),
        sa.Column("setup_token_ref", sa.String(length=255), nullable=False),
        sa.Column("provider_customer_ref", sa.String(length=255), nullable=True),
        sa.Column("payment_method_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["payment_method_id"],
            ["reseller_payment_methods.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"], ["resellers.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("setup_token_ref"),
    )
    op.create_index(
        "ix_paypal_pending_setups_id",
        "paypal_pending_setups",
        ["id"],
    )
    op.create_index(
        "ix_paypal_pending_setups_reseller_id",
        "paypal_pending_setups",
        ["reseller_id"],
    )
    op.create_index(
        "ix_paypal_pending_setups_payment_method_id",
        "paypal_pending_setups",
        ["payment_method_id"],
    )
    op.create_index(
        "ix_paypal_pending_setups_reseller_expires",
        "paypal_pending_setups",
        ["reseller_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table("paypal_pending_setups")
