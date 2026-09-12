"""Add per-server Serial-over-LAN profile.

Revision ID: add_sol_profile
Revises: commerce_platform_phase2
Create Date: 2026-09-11
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_sol_profile"
down_revision: Union[str, Sequence[str], None] = "commerce_platform_phase2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "servers" not in inspector.get_table_names():
        return
    if "sol_profile" not in _column_names(inspector, "servers"):
        op.add_column(
            "servers",
            sa.Column("sol_profile", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "servers" not in inspector.get_table_names():
        return
    if "sol_profile" in _column_names(inspector, "servers"):
        op.drop_column("servers", "sol_profile")
