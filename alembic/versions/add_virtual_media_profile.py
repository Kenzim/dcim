"""Add per-server BMC virtual-media profile.

Revision ID: add_virtual_media_profile
Revises: add_sol_profile
Create Date: 2026-09-12
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_virtual_media_profile"
down_revision: Union[str, Sequence[str], None] = "add_sol_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "servers" not in inspector.get_table_names():
        return
    if "virtual_media_profile" not in _column_names(inspector, "servers"):
        op.add_column(
            "servers",
            sa.Column("virtual_media_profile", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "servers" not in inspector.get_table_names():
        return
    if "virtual_media_profile" in _column_names(inspector, "servers"):
        op.drop_column("servers", "virtual_media_profile")
