"""add proxy_subnet_groups and members

Revision ID: add_proxy_subnet_groups
Revises: add_proxy_runners_table
Create Date: 2026-07-30 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_proxy_subnet_groups"
down_revision: Union[str, Sequence[str], None] = "add_proxy_runners_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_table(inspector, "proxy_subnet_groups"):
        op.create_table(
            "proxy_subnet_groups",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("code", sa.String(length=128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("code", name="uq_proxy_subnet_groups_code"),
        )
        op.create_index("ix_proxy_subnet_groups_id", "proxy_subnet_groups", ["id"])
        op.create_index("ix_proxy_subnet_groups_code", "proxy_subnet_groups", ["code"])

    inspector = sa.inspect(conn)
    if not _has_table(inspector, "proxy_subnet_group_members"):
        op.create_table(
            "proxy_subnet_group_members",
            sa.Column("group_id", sa.Integer(), nullable=False),
            sa.Column("subnet_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["group_id"], ["proxy_subnet_groups.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["subnet_id"], ["ip_subnets.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("group_id", "subnet_id"),
            sa.UniqueConstraint("group_id", "subnet_id", name="uq_proxy_subnet_group_member"),
        )
        op.create_index("ix_proxy_subnet_group_members_subnet_id", "proxy_subnet_group_members", ["subnet_id"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_table(inspector, "proxy_subnet_group_members"):
        op.drop_index("ix_proxy_subnet_group_members_subnet_id", table_name="proxy_subnet_group_members")
        op.drop_table("proxy_subnet_group_members")
    inspector = sa.inspect(conn)
    if _has_table(inspector, "proxy_subnet_groups"):
        op.drop_index("ix_proxy_subnet_groups_code", table_name="proxy_subnet_groups")
        op.drop_index("ix_proxy_subnet_groups_id", table_name="proxy_subnet_groups")
        op.drop_table("proxy_subnet_groups")
