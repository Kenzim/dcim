"""service_vm.vm_ip_allocation_id — canonical VM ↔ pool IP link.

Revision ID: svc_vm_vm_ip_alloc
Revises: vm_ip_alloc_svc
Create Date: 2026-03-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "svc_vm_vm_ip_alloc"
down_revision: Union[str, Sequence[str], None] = "vm_ip_alloc_svc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "service_vm",
        sa.Column("vm_ip_allocation_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_service_vm_vm_ip_allocation_id"),
        "service_vm",
        ["vm_ip_allocation_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_service_vm_vm_ip_allocation_id_vm_ip_allocations"),
        "service_vm",
        "vm_ip_allocations",
        ["vm_ip_allocation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Backfill from pool rows that already point at this service (pre-migration data).
    op.execute(
        sa.text(
            """
            UPDATE service_vm
            SET vm_ip_allocation_id = (
                SELECT v.id FROM vm_ip_allocations AS v
                WHERE v.assigned_service_id = service_vm.service_id
                LIMIT 1
            )
            WHERE EXISTS (
                SELECT 1 FROM vm_ip_allocations AS v2
                WHERE v2.assigned_service_id = service_vm.service_id
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_service_vm_vm_ip_allocation_id_vm_ip_allocations"),
        "service_vm",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_service_vm_vm_ip_allocation_id"), table_name="service_vm")
    op.drop_column("service_vm", "vm_ip_allocation_id")
