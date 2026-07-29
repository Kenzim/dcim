"""Align reseller client product permissions with current model.

Revision ID: reseller_platform_phase9_client_perms
Revises: reseller_platform_phase8_enum_widths
Create Date: 2026-07-29

An earlier draft of this table keyed permissions by client user and used an
``allowed`` flag. The shipping model is reseller+product visibility with a
JSON permissions object, so rebuild the empty table to match.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "reseller_platform_phase9_client_perms"
down_revision: Union[str, None] = "reseller_platform_phase8_enum_widths"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("reseller_client_product_permissions")
    op.create_table(
        "reseller_client_product_permissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column(
            "visible", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("permissions", sa.JSON(), nullable=False),
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
            ["product_id"],
            ["products.id"],
            name="fk_reseller_client_permissions_product_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            ["resellers.id"],
            name="fk_reseller_client_permissions_reseller_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reseller_id",
            "product_id",
            name="uq_reseller_client_product_permission",
        ),
    )
    op.create_index(
        "ix_reseller_client_permissions_reseller_id",
        "reseller_client_product_permissions",
        ["reseller_id"],
    )
    op.create_index(
        "ix_reseller_client_permissions_product_id",
        "reseller_client_product_permissions",
        ["product_id"],
    )


def downgrade() -> None:
    op.drop_table("reseller_client_product_permissions")
    op.create_table(
        "reseller_client_product_permissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=False),
        sa.Column("client_user_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column(
            "allowed", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("permissions", sa.JSON(), nullable=False),
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
            ["client_user_id"],
            ["users.id"],
            name="fk_reseller_client_permissions_client_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_reseller_client_permissions_product_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            ["resellers.id"],
            name="fk_reseller_client_permissions_reseller_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reseller_id",
            "client_user_id",
            "product_id",
            name="uq_reseller_client_product_permission",
        ),
    )
    op.create_index(
        "ix_reseller_client_permissions_reseller_id",
        "reseller_client_product_permissions",
        ["reseller_id"],
    )
    op.create_index(
        "ix_reseller_client_permissions_client_user_id",
        "reseller_client_product_permissions",
        ["client_user_id"],
    )
    op.create_index(
        "ix_reseller_client_permissions_product_id",
        "reseller_client_product_permissions",
        ["product_id"],
    )
