"""add vm ip allocation tables

Revision ID: add_vm_ips
Revises: add_vm_tpls
Create Date: 2026-03-20 17:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_vm_ips"
down_revision: Union[str, Sequence[str], None] = "add_vm_tpls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vm_ip_allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=False),
        sa.Column("subnet_mask", sa.String(length=64), nullable=False),
        sa.Column("gateway", sa.String(length=64), nullable=False),
        sa.Column("bridge_name", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ip_address", name="uq_vm_ip_allocations_ip"),
    )
    op.create_index(op.f("ix_vm_ip_allocations_id"), "vm_ip_allocations", ["id"], unique=False)
    op.create_index(op.f("ix_vm_ip_allocations_ip_address"), "vm_ip_allocations", ["ip_address"], unique=True)

    op.create_table(
        "vm_ip_allocation_clusters",
        sa.Column("vm_ip_allocation_id", sa.Integer(), nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["vm_ip_allocation_id"], ["vm_ip_allocations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cluster_id"], ["proxmox_clusters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("vm_ip_allocation_id", "cluster_id"),
    )


def downgrade() -> None:
    op.drop_table("vm_ip_allocation_clusters")
    op.drop_index(op.f("ix_vm_ip_allocations_ip_address"), table_name="vm_ip_allocations")
    op.drop_index(op.f("ix_vm_ip_allocations_id"), table_name="vm_ip_allocations")
    op.drop_table("vm_ip_allocations")
