"""Drop unused OS profile catalog tables.

Revision ID: drop_os_profiles
Revises: add_virtual_media_profile
Create Date: 2026-09-14
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "drop_os_profiles"
down_revision: Union[str, Sequence[str], None] = "add_virtual_media_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "product_family_os_profiles" in tables:
        op.drop_table("product_family_os_profiles")
    if "os_profiles" in tables:
        op.drop_table("os_profiles")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "os_profiles" not in tables:
        op.create_table(
            "os_profiles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(128), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("os_family", sa.String(64), nullable=False),
            sa.Column("strategy_name", sa.String(128), nullable=True),
            sa.Column("strategy_config", sa.JSON(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("code", name="uq_os_profiles_code"),
        )
    if "product_family_os_profiles" not in tables:
        op.create_table(
            "product_family_os_profiles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("family_id", sa.Integer(), sa.ForeignKey("product_families.id", ondelete="CASCADE"), nullable=False),
            sa.Column("os_profile_id", sa.Integer(), sa.ForeignKey("os_profiles.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("family_id", "os_profile_id", name="uq_family_os_profile"),
        )
