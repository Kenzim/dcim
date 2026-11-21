"""Private networks: storage presence and DHCP

Adds the DHCP settings and presence columns to private networks, and the owner reservation
and dynamic lease tables. See docs/private-network-dhcp.md.

Revision ID: private_network_dhcp
Revises: storage_failover
Create Date: 2026-11-20 18:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "private_network_dhcp"
down_revision: Union[str, Sequence[str], None] = "storage_failover"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _presence_node_column():
    """SQLite cannot add a foreign key to an existing table, so there the column is plain."""
    keys = [] if op.get_bind().dialect.name == "sqlite" else [sa.ForeignKey("storage_nodes.id", ondelete="SET NULL")]
    return sa.Column("presence_node_id", sa.Integer(), *keys, nullable=True)


_NETWORK_COLUMNS = (
    ("dhcp_enabled", lambda: sa.Column("dhcp_enabled", sa.Boolean(), nullable=False, server_default=sa.true())),
    ("dhcp_range_start", lambda: sa.Column("dhcp_range_start", sa.String(15), nullable=True)),
    ("dhcp_range_end", lambda: sa.Column("dhcp_range_end", sa.String(15), nullable=True)),
    ("dhcp_lease_seconds", lambda: sa.Column("dhcp_lease_seconds", sa.Integer(), nullable=True)),
    ("dhcp_options", lambda: sa.Column("dhcp_options", sa.JSON(), nullable=True)),
    ("presence_node_id", _presence_node_column),
    ("presence_since", lambda: sa.Column("presence_since", sa.DateTime(timezone=True), nullable=True)),
    ("dhcp_error", lambda: sa.Column("dhcp_error", sa.String(2000), nullable=True)),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    present = {c["name"] for c in inspector.get_columns("private_networks")}
    for name, make in _NETWORK_COLUMNS:
        if name not in present:
            op.add_column("private_networks", make())
    tables = set(inspector.get_table_names())

    if "private_network_dhcp_reservations" not in tables:
        op.create_table(
            "private_network_dhcp_reservations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "network_id",
                sa.Integer(),
                sa.ForeignKey("private_networks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("mac", sa.String(17), nullable=False),
            sa.Column("ip", sa.String(15), nullable=False),
            sa.Column("label", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("network_id", "mac", name="uq_private_network_dhcp_reservation_mac"),
            sa.UniqueConstraint("network_id", "ip", name="uq_private_network_dhcp_reservation_ip"),
        )
        op.create_index("ix_private_network_dhcp_reservations_id", "private_network_dhcp_reservations", ["id"])
        op.create_index(
            "ix_private_network_dhcp_reservations_network_id", "private_network_dhcp_reservations", ["network_id"]
        )

    if "private_network_dhcp_leases" not in tables:
        op.create_table(
            "private_network_dhcp_leases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "network_id",
                sa.Integer(),
                sa.ForeignKey("private_networks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("mac", sa.String(17), nullable=False),
            sa.Column("ip", sa.String(15), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("hostname", sa.String(64), nullable=True),
            sa.Column(
                "node_id", sa.Integer(), sa.ForeignKey("storage_nodes.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("network_id", "mac", name="uq_private_network_dhcp_lease_mac"),
        )
        op.create_index("ix_private_network_dhcp_leases_id", "private_network_dhcp_leases", ["id"])
        op.create_index("ix_private_network_dhcp_leases_network_id", "private_network_dhcp_leases", ["network_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("private_network_dhcp_leases", "private_network_dhcp_reservations"):
        if table in tables:
            op.drop_table(table)
    present = {c["name"] for c in inspector.get_columns("private_networks")}
    names = [name for name, _make in reversed(_NETWORK_COLUMNS) if name in present]
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("private_networks") as batch:
            for name in names:
                batch.drop_column(name)
        return
    for name in names:
        for fk in inspector.get_foreign_keys("private_networks"):
            if fk["constrained_columns"] == [name] and fk.get("name"):
                op.drop_constraint(fk["name"], "private_networks", type_="foreignkey")
        op.drop_column("private_networks", name)
