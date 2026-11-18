"""add SFTP access to NFS storage: node endpoint, share access mode, SFTP users

Revision ID: add_storage_sftp
Revises: add_switch_vpc_pairing
Create Date: 2026-11-18 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_storage_sftp"
down_revision: Union[str, Sequence[str], None] = "add_switch_vpc_pairing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NODE_COLUMNS = (
    ("sftp_enabled", lambda: sa.Column("sftp_enabled", sa.Boolean(), nullable=False, server_default=sa.false())),
    ("sftp_host", lambda: sa.Column("sftp_host", sa.String(255), nullable=True)),
    ("sftp_port", lambda: sa.Column("sftp_port", sa.Integer(), nullable=False, server_default="2222")),
    ("sftp_listening", lambda: sa.Column("sftp_listening", sa.Boolean(), nullable=True)),
    ("sftp_host_key", lambda: sa.Column("sftp_host_key", sa.Text(), nullable=True)),
    ("sftp_host_key_fingerprint", lambda: sa.Column("sftp_host_key_fingerprint", sa.String(128), nullable=True)),
    ("sftp_error", lambda: sa.Column("sftp_error", sa.Text(), nullable=True)),
)
_TENANT_COLUMNS = (
    ("sftp_state", lambda: sa.Column("sftp_state", sa.String(16), nullable=True)),
    ("sftp_error", lambda: sa.Column("sftp_error", sa.Text(), nullable=True)),
)
_SHARE_COLUMNS = (
    ("sftp_access", lambda: sa.Column("sftp_access", sa.String(8), nullable=False, server_default="off")),
)


def _columns(inspector, table_name: str) -> set:
    return {c["name"] for c in inspector.get_columns(table_name)}


def _add_missing(inspector, table: str, columns) -> None:
    present = _columns(inspector, table)
    for name, make in columns:
        if name not in present:
            op.add_column(table, make())


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    _add_missing(inspector, "storage_nodes", _NODE_COLUMNS)
    _add_missing(inspector, "storage_tenants", _TENANT_COLUMNS)
    _add_missing(inspector, "storage_shares", _SHARE_COLUMNS)

    if "storage_sftp_users" not in set(inspector.get_table_names()):
        op.create_table(
            "storage_sftp_users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id", sa.Integer(), sa.ForeignKey("storage_tenants.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("name", sa.String(32), nullable=False),
            sa.Column("public_keys", sa.JSON(), nullable=True),
            sa.Column("password_hash", sa.String(255), nullable=True),
            sa.Column("allowed_ips", sa.JSON(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_login_ip", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("tenant_id", "name", name="uq_storage_sftp_user_name"),
        )
        op.create_index("ix_storage_sftp_users_id", "storage_sftp_users", ["id"])
        op.create_index("ix_storage_sftp_users_tenant_id", "storage_sftp_users", ["tenant_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "storage_sftp_users" in set(inspector.get_table_names()):
        op.drop_table("storage_sftp_users")
    for table, columns in (
        ("storage_shares", _SHARE_COLUMNS),
        ("storage_tenants", _TENANT_COLUMNS),
        ("storage_nodes", _NODE_COLUMNS),
    ):
        present = _columns(inspector, table)
        for name, _make in columns:
            if name in present:
                op.drop_column(table, name)
