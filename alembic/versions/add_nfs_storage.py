"""add NFS storage nodes, tenants, shares and private network reserved ranges

Revision ID: add_nfs_storage
Revises: add_private_networks
Create Date: 2026-11-18 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_nfs_storage"
down_revision: Union[str, Sequence[str], None] = "add_private_networks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table_name: str) -> set:
    return {c["name"] for c in inspector.get_columns(table_name)}


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "reserved_cidrs" not in _columns(inspector, "private_network_pools"):
        op.add_column("private_network_pools", sa.Column("reserved_cidrs", sa.JSON(), nullable=True))

    if "storage_nodes" not in tables:
        op.create_table(
            "storage_nodes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("server_id", sa.Integer(), sa.ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
            sa.Column("api_key_hash", sa.String(64), nullable=False),
            sa.Column("api_key_prefix", sa.String(16), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("vlan_mode", sa.String(16), nullable=False, server_default="switch"),
            sa.Column("vlan_port_ids", sa.JSON(), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("last_seen_ip", sa.String(64), nullable=True),
            sa.Column("runner_version", sa.String(64), nullable=True),
            sa.Column("ceph_ok", sa.Boolean(), nullable=True),
            sa.Column("free_bytes", sa.BigInteger(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("version_applied", sa.String(64), nullable=True),
            sa.Column("orphans", sa.JSON(), nullable=True),
            *_timestamps(),
            sa.UniqueConstraint("api_key_hash", name="uq_storage_nodes_api_key_hash"),
        )
        op.create_index("ix_storage_nodes_id", "storage_nodes", ["id"])
        op.create_index("ix_storage_nodes_server_id", "storage_nodes", ["server_id"], unique=True)

    if "storage_tenants" not in tables:
        op.create_table(
            "storage_tenants",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="SET NULL"), nullable=True),
            sa.Column(
                "node_id", sa.Integer(), sa.ForeignKey("storage_nodes.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column(
                "network_id",
                sa.Integer(),
                sa.ForeignKey("private_networks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="SET NULL"), nullable=True),
            sa.Column("vlan", sa.Integer(), nullable=True),
            sa.Column("network_cidr", sa.String(32), nullable=True),
            sa.Column("endpoint_ip", sa.String(45), nullable=True),
            sa.Column("size_bytes", sa.BigInteger(), nullable=False),
            sa.Column("limits", sa.JSON(), nullable=True),
            sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("destroy_after", sa.DateTime(timezone=True), nullable=True),
            sa.Column("runner_state", sa.String(16), nullable=True),
            sa.Column("runner_error", sa.Text(), nullable=True),
            *_timestamps(),
        )
        op.create_index("ix_storage_tenants_id", "storage_tenants", ["id"])
        op.create_index("ix_storage_tenants_service_id", "storage_tenants", ["service_id"], unique=True)
        op.create_index("ix_storage_tenants_node_id", "storage_tenants", ["node_id"])
        op.create_index("ix_storage_tenants_network_id", "storage_tenants", ["network_id"], unique=True)
        op.create_index("ix_storage_tenants_location_id", "storage_tenants", ["location_id"])

    if "storage_shares" not in tables:
        op.create_table(
            "storage_shares",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id", sa.Integer(), sa.ForeignKey("storage_tenants.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("name", sa.String(32), nullable=False),
            sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
            sa.Column("clients", sa.JSON(), nullable=True),
            sa.Column("all_clients", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("state", sa.String(16), nullable=False, server_default="active"),
            sa.Column("destroy_after", sa.DateTime(timezone=True), nullable=True),
            sa.Column("used_bytes", sa.BigInteger(), nullable=True),
            sa.Column("runner_state", sa.String(16), nullable=True),
            sa.Column("runner_error", sa.Text(), nullable=True),
            *_timestamps(),
            sa.UniqueConstraint("tenant_id", "name", name="uq_storage_share_name"),
        )
        op.create_index("ix_storage_shares_id", "storage_shares", ["id"])
        op.create_index("ix_storage_shares_tenant_id", "storage_shares", ["tenant_id"])
        op.create_index("ix_storage_shares_state", "storage_shares", ["state"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("storage_shares", "storage_tenants", "storage_nodes"):
        if table in tables:
            op.drop_table(table)
    if "reserved_cidrs" in _columns(inspector, "private_network_pools"):
        op.drop_column("private_network_pools", "reserved_cidrs")
