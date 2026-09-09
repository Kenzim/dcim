"""Commerce phase 2: webhooks + retail ServiceBilling / invoice nullability.

Revision ID: commerce_platform_phase2
Revises: commerce_platform_phase1
Create Date: 2026-07-31

Applies pieces that may have been added to phase1 after it was already stamped.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "commerce_platform_phase2"
down_revision: Union[str, None] = "commerce_platform_phase1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ON_DELETE_SET_NULL = "SET NULL"


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _column_names(inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "invoices"):
        op.alter_column(
            "invoices",
            "reseller_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    inspector = sa.inspect(bind)
    if _has_table(inspector, "service_billings"):
        columns = _column_names(inspector, "service_billings")
        if "billing_account_id" not in columns:
            op.add_column(
                "service_billings",
                sa.Column("billing_account_id", sa.Integer(), nullable=True),
            )
            op.create_foreign_key(
                "fk_service_billings_billing_account_id",
                "service_billings",
                "billing_accounts",
                ["billing_account_id"],
                ["id"],
                ondelete=_ON_DELETE_SET_NULL,
            )
            op.create_index(
                "ix_service_billings_billing_account_id",
                "service_billings",
                ["billing_account_id"],
            )
        op.alter_column(
            "service_billings",
            "reseller_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "webhook_endpoints"):
        op.create_table(
            "webhook_endpoints",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("url", sa.String(length=2048), nullable=False),
            sa.Column("secret_hash", sa.String(length=128), nullable=False),
            sa.Column("events", sa.JSON(), nullable=False),
            sa.Column(
                "enabled",
                sa.Boolean(),
                server_default=sa.text("1"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_webhook_endpoints_id", "webhook_endpoints", ["id"])

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "webhook_deliveries"):
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("endpoint_id", sa.Integer(), nullable=False),
            sa.Column("event", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=32),
                server_default="pending",
                nullable=False,
            ),
            sa.Column(
                "attempts",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["endpoint_id"],
                ["webhook_endpoints.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_webhook_deliveries_id", "webhook_deliveries", ["id"])
        op.create_index(
            "ix_webhook_deliveries_endpoint_id",
            "webhook_deliveries",
            ["endpoint_id"],
        )
        op.create_index(
            "ix_webhook_deliveries_event", "webhook_deliveries", ["event"]
        )
        op.create_index(
            "ix_webhook_deliveries_status", "webhook_deliveries", ["status"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_table(inspector, "webhook_deliveries"):
        op.drop_table("webhook_deliveries")
    inspector = sa.inspect(bind)
    if _has_table(inspector, "webhook_endpoints"):
        op.drop_table("webhook_endpoints")
    # Do not re-tighten reseller_id nullability on downgrade (data may exist).
