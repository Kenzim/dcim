"""add persistent vmid reservations table

Revision ID: add_vmid_resv
Revises: svc_owner_vm_lifecycle
Create Date: 2026-03-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_vmid_resv"
down_revision: Union[str, Sequence[str], None] = "svc_owner_vm_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_table(inspector, "vmid_reservations"):
        return
    op.create_table(
        "vmid_reservations",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("vmid", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["proxmox_clusters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("cluster_id", "vmid", name="uq_vmid_reservations_cluster_vmid"),
    )
    op.create_index("ix_vmid_reservations_id", "vmid_reservations", ["id"])
    op.create_index("ix_vmid_reservations_cluster_id", "vmid_reservations", ["cluster_id"])
    op.create_index("ix_vmid_reservations_service_id", "vmid_reservations", ["service_id"])
    op.create_index("ix_vmid_reservations_vmid", "vmid_reservations", ["vmid"])

    # Backfill from existing VM placements where both cluster/vmid are set.
    op.execute(
        sa.text(
            """
            INSERT INTO vmid_reservations (cluster_id, service_id, vmid)
            SELECT sv.proxmox_cluster_id, sv.service_id, sv.proxmox_vmid
            FROM service_vm sv
            WHERE sv.proxmox_cluster_id IS NOT NULL
              AND sv.proxmox_vmid IS NOT NULL
            ON CONFLICT DO NOTHING
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _has_table(inspector, "vmid_reservations"):
        op.drop_table("vmid_reservations")
