"""add unified runners table for WebSocket phone-home agents

Revision ID: add_runners_table
Revises: drop_os_profiles
Create Date: 2026-09-19 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_runners_table"
down_revision: Union[str, Sequence[str], None] = "drop_os_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_table(inspector, "runners"):
        return
    op.create_table(
        "runners",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_ip", sa.String(length=64), nullable=True),
        sa.Column("agent_version", sa.String(length=64), nullable=True),
        sa.Column("state", sa.JSON(), nullable=True),
        sa.Column("state_updated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_runners_id", "runners", ["id"])
    op.create_index("ix_runners_location_id", "runners", ["location_id"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not _has_table(inspector, "runners"):
        return
    op.drop_index("ix_runners_location_id", table_name="runners")
    op.drop_index("ix_runners_id", table_name="runners")
    op.drop_table("runners")
