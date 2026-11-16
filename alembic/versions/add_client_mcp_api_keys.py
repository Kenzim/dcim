"""Add client_mcp_api_keys table for the client MCP endpoint (/mcp/client).

Revision ID: add_client_mcp_api_keys
Revises: add_server_lock
Create Date: 2026-11-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_client_mcp_api_keys"
down_revision: Union[str, Sequence[str], None] = "add_server_lock"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "client_mcp_api_keys"):
        return
    op.create_table(
        "client_mcp_api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("api_key", sa.String(length=64), nullable=False),
        sa.Column("api_key_prefix", sa.String(length=16), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(length=45), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_client_mcp_api_keys_user_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_client_mcp_api_keys_id", "client_mcp_api_keys", ["id"])
    op.create_index("ix_client_mcp_api_keys_user_id", "client_mcp_api_keys", ["user_id"])
    op.create_index("ix_client_mcp_api_keys_enabled", "client_mcp_api_keys", ["enabled"])
    op.create_index("ix_client_mcp_api_keys_api_key", "client_mcp_api_keys", ["api_key"], unique=True)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _has_table(inspector, "client_mcp_api_keys"):
        return
    op.drop_index("ix_client_mcp_api_keys_api_key", table_name="client_mcp_api_keys")
    op.drop_index("ix_client_mcp_api_keys_enabled", table_name="client_mcp_api_keys")
    op.drop_index("ix_client_mcp_api_keys_user_id", table_name="client_mcp_api_keys")
    op.drop_index("ix_client_mcp_api_keys_id", table_name="client_mcp_api_keys")
    op.drop_table("client_mcp_api_keys")
