"""Add missing reseller_payment_methods.method_type column.

Revision ID: reseller_platform_phase11_payment_method_type
Revises: reseller_platform_phase10_column_rename
Create Date: 2026-07-29
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "reseller_platform_phase11_payment_method_type"
down_revision: Union[str, None] = "reseller_platform_phase10_column_rename"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("reseller_payment_methods")}
    if "method_type" in columns:
        return

    op.add_column(
        "reseller_payment_methods",
        sa.Column("method_type", sa.String(length=64), nullable=True),
    )
    # Backfill from provider: Stripe methods are cards; others match provider name.
    op.execute(
        sa.text(
            """
            UPDATE reseller_payment_methods
            SET method_type = CASE
                WHEN provider = 'stripe' THEN 'card'
                WHEN provider = 'paypal' THEN 'paypal'
                ELSE provider
            END
            WHERE method_type IS NULL OR method_type = ''
            """
        )
    )
    op.execute(
        sa.text(
            "UPDATE reseller_payment_methods SET method_type = 'unknown' "
            "WHERE method_type IS NULL OR method_type = ''"
        )
    )
    op.alter_column(
        "reseller_payment_methods",
        "method_type",
        existing_type=sa.String(length=64),
        nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("reseller_payment_methods")}
    if "method_type" not in columns:
        return
    op.drop_column("reseller_payment_methods", "method_type")
