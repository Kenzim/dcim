"""service vm placement and provisioning_source

Revision ID: svc_vm_place
Revises: add_vm_ips
Create Date: 2026-03-20 20:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "svc_vm_place"
down_revision: Union[str, Sequence[str], None] = "add_vm_ips"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table: str, column: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return column in [c["name"] for c in inspector.get_columns(table)]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_column(inspector, "services", "provisioning_source"):
        op.add_column(
            "services",
            sa.Column(
                "provisioning_source",
                sa.String(length=32),
                nullable=False,
                server_default="billing",
            ),
        )

    if not _has_column(inspector, "services", "proxmox_cluster_id"):
        op.add_column(
            "services",
            sa.Column("proxmox_cluster_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_services_proxmox_cluster_id",
            "services",
            "proxmox_clusters",
            ["proxmox_cluster_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index("ix_services_proxmox_cluster_id", "services", ["proxmox_cluster_id"])

    if not _has_column(inspector, "services", "proxmox_node_name"):
        op.add_column(
            "services",
            sa.Column("proxmox_node_name", sa.String(length=255), nullable=True),
        )

    if not _has_column(inspector, "services", "proxmox_vmid"):
        op.add_column(
            "services",
            sa.Column("proxmox_vmid", sa.Integer(), nullable=True),
        )

    # Allow internal/test services without external_users row
    if _has_column(inspector, "services", "external_user_id"):
        if conn.dialect.name == "sqlite":
            with op.batch_alter_table("services") as batch_op:
                batch_op.alter_column(
                    "external_user_id",
                    existing_type=sa.Integer(),
                    nullable=True,
                )
        else:
            op.alter_column(
                "services",
                "external_user_id",
                existing_type=sa.Integer(),
                nullable=True,
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if _has_column(inspector, "services", "external_user_id"):
        if conn.dialect.name == "sqlite":
            with op.batch_alter_table("services") as batch_op:
                batch_op.alter_column(
                    "external_user_id",
                    existing_type=sa.Integer(),
                    nullable=False,
                )
        else:
            op.alter_column(
                "services",
                "external_user_id",
                existing_type=sa.Integer(),
                nullable=False,
            )

    if _has_column(inspector, "services", "proxmox_vmid"):
        op.drop_column("services", "proxmox_vmid")

    if _has_column(inspector, "services", "proxmox_node_name"):
        op.drop_column("services", "proxmox_node_name")

    if _has_column(inspector, "services", "proxmox_cluster_id"):
        op.drop_index("ix_services_proxmox_cluster_id", table_name="services")
        op.drop_constraint("fk_services_proxmox_cluster_id", "services", type_="foreignkey")
        op.drop_column("services", "proxmox_cluster_id")

    if _has_column(inspector, "services", "provisioning_source"):
        op.drop_column("services", "provisioning_source")
