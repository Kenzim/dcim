"""Add reseller provisioning idempotency and client identity scope.

Revision ID: reseller_platform_phase2
Revises: reseller_platform_phase1
Create Date: 2026-07-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "reseller_platform_phase2"
down_revision: Union[str, None] = "reseller_platform_phase1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_users_reseller_identity",
        "users",
        ["reseller_id", "external_user_id"],
    )
    op.create_table(
        "reseller_provisioning_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=True),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "processing",
                "insufficient_credit",
                "succeeded",
                "failed",
                native_enum=False,
            ),
            server_default="processing",
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
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
            ["invoice_id"],
            ["invoices.id"],
            name="fk_reseller_provisioning_request_invoice_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            ["resellers.id"],
            name="fk_reseller_provisioning_request_reseller_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name="fk_reseller_provisioning_request_service_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reseller_id",
            "idempotency_key",
            name="uq_reseller_provisioning_request_key",
        ),
    )
    op.create_index(
        "ix_reseller_provisioning_requests_reseller_id",
        "reseller_provisioning_requests",
        ["reseller_id"],
    )
    op.create_index(
        "ix_reseller_provisioning_requests_invoice_id",
        "reseller_provisioning_requests",
        ["invoice_id"],
    )
    op.create_index(
        "ix_reseller_provisioning_requests_service_id",
        "reseller_provisioning_requests",
        ["service_id"],
    )
    op.create_index(
        "ix_reseller_provisioning_requests_status",
        "reseller_provisioning_requests",
        ["status"],
    )
    op.create_index(
        "ix_reseller_provisioning_request_status",
        "reseller_provisioning_requests",
        ["reseller_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("reseller_provisioning_requests")
    op.drop_constraint(
        "uq_users_reseller_identity", "users", type_="unique"
    )
