"""proxy fast path: runner location and pinning, proxy access networks

Revision ID: add_proxy_fast_path
Revises: add_nfs_storage
Create Date: 2026-11-18 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_proxy_fast_path"
down_revision: Union[str, Sequence[str], None] = "add_nfs_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, referenced table, foreign key name)
_FOREIGN_KEYS = (
    ("proxy_runners", "location_id", "locations", "fk_proxy_runners_location_id"),
    ("proxy_runners", "server_id", "servers", "fk_proxy_runners_server_id"),
    ("ip_subnets", "proxy_runner_id", "proxy_runners", "fk_ip_subnets_proxy_runner_id"),
    (
        "private_network_attachments",
        "proxy_runner_id",
        "proxy_runners",
        "fk_private_network_attachments_proxy_runner_id",
    ),
)


def _columns(inspector, table_name: str) -> set:
    return {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    for table, column, target, fk_name in _FOREIGN_KEYS:
        if column in _columns(inspector, table):
            continue
        op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
        op.create_index(op.f(f"ix_{table}_{column}"), table, [column], unique=False)
        op.create_foreign_key(fk_name, table, target, [column], ["id"], ondelete="SET NULL")

    if "vlan_mode" not in _columns(inspector, "proxy_runners"):
        op.add_column(
            "proxy_runners",
            sa.Column("vlan_mode", sa.String(length=13), nullable=False, server_default="preconfigured"),
        )
    if "fast_path_ip" not in _columns(inspector, "ip_subnets"):
        op.add_column("ip_subnets", sa.Column("fast_path_ip", sa.String(length=64), nullable=True))
        op.create_unique_constraint("uq_ip_subnets_fast_path_ip", "ip_subnets", ["fast_path_ip"])

    pool_columns = _columns(inspector, "private_network_pools")
    for name in ("proxy_access_enabled", "proxy_access_requires_product"):
        if name not in pool_columns:
            op.add_column(
                "private_network_pools", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false())
            )

    if "kind" not in _columns(inspector, "private_networks"):
        op.add_column(
            "private_networks", sa.Column("kind", sa.String(length=12), nullable=False, server_default="standard")
        )
        op.create_index(op.f("ix_private_networks_kind"), "private_networks", ["kind"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_private_networks_kind"), table_name="private_networks")
    op.drop_column("private_networks", "kind")
    op.drop_column("private_network_pools", "proxy_access_requires_product")
    op.drop_column("private_network_pools", "proxy_access_enabled")
    op.drop_constraint("uq_ip_subnets_fast_path_ip", "ip_subnets", type_="unique")
    op.drop_column("ip_subnets", "fast_path_ip")
    op.drop_column("proxy_runners", "vlan_mode")
    for table, column, _target, fk_name in reversed(_FOREIGN_KEYS):
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.drop_index(op.f(f"ix_{table}_{column}"), table_name=table)
        op.drop_column(table, column)
