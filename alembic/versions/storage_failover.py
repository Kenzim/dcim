"""NFS storage failover: tenant current node and moves, node HA state, locations and events

Adds the failover columns to storage tenants and nodes, the per-location storage settings
(failover and the shared SFTP endpoint, seeded from the nodes that already offer SFTP) and the
failover event log. See docs/nfs-storage-failover.md.

Revision ID: storage_failover
Revises: alerting_tables
Create Date: 2026-11-19 20:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "storage_failover"
down_revision: Union[str, Sequence[str], None] = "alerting_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _node_fk(column: str):
    """SQLite cannot add a foreign key to an existing table, so there the column is plain."""

    def make():
        keys = [] if op.get_bind().dialect.name == "sqlite" else [sa.ForeignKey("storage_nodes.id", ondelete="SET NULL")]
        return sa.Column(column, sa.Integer(), *keys, nullable=True)

    return make


_TENANT_COLUMNS = (
    ("current_node_id", _node_fk("current_node_id")),
    ("current_since", lambda: sa.Column("current_since", sa.DateTime(timezone=True), nullable=True)),
    ("move_to_node_id", _node_fk("move_to_node_id")),
    ("move_id", lambda: sa.Column("move_id", sa.String(32), nullable=True)),
    ("move_requested_at", lambda: sa.Column("move_requested_at", sa.DateTime(timezone=True), nullable=True)),
    ("move_set_preferred", lambda: sa.Column("move_set_preferred", sa.Boolean(), nullable=False, server_default=sa.true())),
    ("last_failover_at", lambda: sa.Column("last_failover_at", sa.DateTime(timezone=True), nullable=True)),
    ("last_failover_from_node_id", _node_fk("last_failover_from_node_id")),
)
_NODE_COLUMNS = (
    ("draining", lambda: sa.Column("draining", sa.Boolean(), nullable=False, server_default=sa.false())),
    ("ha_state", lambda: sa.Column("ha_state", sa.JSON(), nullable=True)),
    ("sftp_master", lambda: sa.Column("sftp_master", sa.Boolean(), nullable=False, server_default=sa.false())),
    ("healthy_since", lambda: sa.Column("healthy_since", sa.DateTime(), nullable=True)),
)


def _columns(inspector, table_name: str) -> set:
    return {c["name"] for c in inspector.get_columns(table_name)}


def _add_missing(inspector, table: str, columns) -> None:
    present = _columns(inspector, table)
    for name, make in columns:
        if name not in present:
            op.add_column(table, make())


def _seed_locations(bind) -> None:
    """One row per location with an SFTP-enabled node, copying that node's host name and port."""
    rows = bind.execute(
        sa.text(
            "SELECT s.location_id, n.sftp_host, n.sftp_port FROM storage_nodes n "
            "JOIN servers s ON s.id = n.server_id "
            "WHERE n.sftp_enabled = :on AND n.enabled = :on AND s.location_id IS NOT NULL ORDER BY n.id"
        ),
        {"on": True},
    ).fetchall()
    seeded = set()
    for location_id, host, port in rows:
        if location_id in seeded:
            continue
        seeded.add(location_id)
        bind.execute(
            sa.text(
                "INSERT INTO storage_locations (location_id, ha_enabled, auto_failback, rebalancing, sftp_enabled, "
                "sftp_host, sftp_port) VALUES (:loc, :on, :on, :off, :on, :host, :port)"
            ),
            {"loc": location_id, "on": True, "off": False, "host": host, "port": port or 2222},
        )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    _add_missing(inspector, "storage_tenants", _TENANT_COLUMNS)
    _add_missing(inspector, "storage_nodes", _NODE_COLUMNS)
    tables = set(inspector.get_table_names())

    if "storage_locations" not in tables:
        op.create_table(
            "storage_locations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "location_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("ha_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("auto_failback", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("rebalancing", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("sftp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("sftp_host", sa.String(255), nullable=True),
            sa.Column("sftp_port", sa.Integer(), nullable=False, server_default="2222"),
            sa.Column("sftp_virtual_ip", sa.String(64), nullable=True),
            sa.Column("sftp_host_key", sa.Text(), nullable=True),
            sa.Column("sftp_host_key_public", sa.Text(), nullable=True),
            sa.Column("sftp_host_key_fingerprint", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("location_id", name="uq_storage_locations_location_id"),
        )
        op.create_index("ix_storage_locations_id", "storage_locations", ["id"])
        _seed_locations(op.get_bind())

    if "storage_events" not in tables:
        op.create_table(
            "storage_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("storage_tenants.id", ondelete="SET NULL"), nullable=True),
            sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="SET NULL"), nullable=True),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("node_id", sa.Integer(), sa.ForeignKey("storage_nodes.id", ondelete="SET NULL"), nullable=True),
            sa.Column("from_node_id", sa.Integer(), sa.ForeignKey("storage_nodes.id", ondelete="SET NULL"), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
        )
        op.create_index("ix_storage_events_id", "storage_events", ["id"])
        op.create_index("ix_storage_events_at", "storage_events", ["at"])
        op.create_index("ix_storage_events_tenant_id", "storage_events", ["tenant_id"])
        op.create_index("ix_storage_events_location_id", "storage_events", ["location_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("storage_events", "storage_locations"):
        if table in tables:
            op.drop_table(table)
    for table, columns in (("storage_nodes", _NODE_COLUMNS), ("storage_tenants", _TENANT_COLUMNS)):
        names = [name for name, _make in reversed(columns) if name in _columns(inspector, table)]
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(table) as batch:
                for name in names:
                    batch.drop_column(name)
            continue
        for name in names:
            for fk in inspector.get_foreign_keys(table):
                if fk["constrained_columns"] == [name] and fk.get("name"):
                    op.drop_constraint(fk["name"], table, type_="foreignkey")
            op.drop_column(table, name)
