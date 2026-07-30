"""add standalone proxy_runners table; migrate proxy service_instances

Revision ID: add_proxy_runners_table
Revises: vm_tpl_shared_storage
Create Date: 2026-07-30 11:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_proxy_runners_table"
down_revision: Union[str, Sequence[str], None] = "vm_tpl_shared_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_table(inspector, "proxy_runners"):
        op.create_table(
            "proxy_runners",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("api_key_encrypted", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("last_seen_ip", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_proxy_runners_id", "proxy_runners", ["id"])

    inspector = sa.inspect(conn)
    if _has_table(inspector, "service_instances"):
        conn.execute(
            sa.text(
                """
                INSERT INTO proxy_runners (name, api_key_encrypted, enabled, created_at, updated_at)
                SELECT name, api_key_encrypted, 1, created_at, updated_at
                FROM service_instances
                WHERE service_type = 'proxy'
                """
            )
        )
        conn.execute(sa.text("DELETE FROM service_instances WHERE service_type = 'proxy'"))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_table(inspector, "proxy_runners"):
        op.drop_index("ix_proxy_runners_id", table_name="proxy_runners")
        op.drop_table("proxy_runners")
