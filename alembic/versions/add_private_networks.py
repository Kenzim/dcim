"""add client private networks and proxmox cluster location

Revision ID: add_private_networks
Revises: add_pdus_power_feeds
Create Date: 2026-11-15 17:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_private_networks"
down_revision: Union[str, Sequence[str], None] = "add_pdus_power_feeds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table_name: str) -> set:
    return {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "location_id" not in _columns(inspector, "proxmox_clusters"):
        op.add_column("proxmox_clusters", sa.Column("location_id", sa.Integer(), nullable=True))
        op.create_index("ix_proxmox_clusters_location_id", "proxmox_clusters", ["location_id"])
        op.create_foreign_key(
            "fk_proxmox_clusters_location_id",
            "proxmox_clusters",
            "locations",
            ["location_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if "private_network_pools" not in tables:
        op.create_table(
            "private_network_pools",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "location_id",
                sa.Integer(),
                sa.ForeignKey("locations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("vlan_start", sa.Integer(), nullable=False),
            sa.Column("vlan_end", sa.Integer(), nullable=False),
            sa.Column("last_allocated", sa.Integer(), nullable=True),
            sa.Column("bridge", sa.String(64), nullable=False, server_default="vmbr0"),
            sa.Column("max_networks_per_client", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("max_networks_per_service", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_private_network_pools_id", "private_network_pools", ["id"])
        op.create_index(
            "ix_private_network_pools_location_id", "private_network_pools", ["location_id"], unique=True
        )

    if "private_networks" not in tables:
        op.create_table(
            "private_networks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column(
                "owner_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id"), nullable=False),
            sa.Column("segmentation_type", sa.String(16), nullable=False, server_default="vlan"),
            sa.Column("segment_id", sa.Integer(), nullable=False),
            sa.Column("cidr", sa.String(32), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("switch_synced", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint(
                "location_id", "segmentation_type", "segment_id", name="uq_private_network_segment"
            ),
        )
        op.create_index("ix_private_networks_id", "private_networks", ["id"])
        op.create_index("ix_private_networks_owner_user_id", "private_networks", ["owner_user_id"])
        op.create_index("ix_private_networks_location_id", "private_networks", ["location_id"])
        op.create_index("ix_private_networks_status", "private_networks", ["status"])

    if "private_network_attachments" not in tables:
        op.create_table(
            "private_network_attachments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "network_id",
                sa.Integer(),
                sa.ForeignKey("private_networks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "service_id",
                sa.Integer(),
                sa.ForeignKey("services.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "server_id",
                sa.Integer(),
                sa.ForeignKey("servers.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("vm_net_index", sa.Integer(), nullable=True),
            sa.Column("mac_address", sa.String(17), nullable=True),
            sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("network_id", "service_id", name="uq_private_network_attachment_service"),
            sa.UniqueConstraint("network_id", "ip_address", name="uq_private_network_attachment_ip"),
        )
        op.create_index("ix_private_network_attachments_id", "private_network_attachments", ["id"])
        op.create_index(
            "ix_private_network_attachments_network_id", "private_network_attachments", ["network_id"]
        )
        op.create_index(
            "ix_private_network_attachments_service_id", "private_network_attachments", ["service_id"]
        )
        op.create_index(
            "ix_private_network_attachments_server_id", "private_network_attachments", ["server_id"]
        )
        op.create_index("ix_private_network_attachments_state", "private_network_attachments", ["state"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("private_network_attachments", "private_networks", "private_network_pools"):
        if table in tables:
            op.drop_table(table)
    if "location_id" in _columns(inspector, "proxmox_clusters"):
        op.drop_constraint("fk_proxmox_clusters_location_id", "proxmox_clusters", type_="foreignkey")
        op.drop_index("ix_proxmox_clusters_location_id", table_name="proxmox_clusters")
        op.drop_column("proxmox_clusters", "location_id")
