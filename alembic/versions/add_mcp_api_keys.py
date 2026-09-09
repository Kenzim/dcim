"""Add mcp_api_keys table for admin MCP Streamable HTTP auth.

Revision ID: add_mcp_api_keys
Revises: add_proxy_subnet_groups
Create Date: 2026-09-07
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_mcp_api_keys"
down_revision: Union[str, Sequence[str], None] = "add_proxy_subnet_groups"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_table(inspector, "mcp_api_keys"):
        op.create_table(
            "mcp_api_keys",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("api_key", sa.String(length=64), nullable=False),
            sa.Column("api_key_prefix", sa.String(length=16), nullable=True),
            sa.Column("scopes", sa.JSON(), nullable=False),
            sa.Column("ip_allowlist", sa.JSON(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
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
                ["created_by_user_id"],
                ["users.id"],
                name="fk_mcp_api_keys_created_by_user_id",
                ondelete="SET NULL",
            ),
        )
        op.create_index("ix_mcp_api_keys_id", "mcp_api_keys", ["id"])
        op.create_index("ix_mcp_api_keys_name", "mcp_api_keys", ["name"])
        op.create_index("ix_mcp_api_keys_enabled", "mcp_api_keys", ["enabled"])
        op.create_index("ix_mcp_api_keys_api_key", "mcp_api_keys", ["api_key"], unique=True)
        op.create_index(
            "ix_mcp_api_keys_created_by_user_id", "mcp_api_keys", ["created_by_user_id"]
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_table(inspector, "mcp_api_keys"):
        op.drop_index("ix_mcp_api_keys_created_by_user_id", table_name="mcp_api_keys")
        op.drop_index("ix_mcp_api_keys_api_key", table_name="mcp_api_keys")
        op.drop_index("ix_mcp_api_keys_enabled", table_name="mcp_api_keys")
        op.drop_index("ix_mcp_api_keys_name", table_name="mcp_api_keys")
        op.drop_index("ix_mcp_api_keys_id", table_name="mcp_api_keys")
        op.drop_table("mcp_api_keys")
