"""vm_ip_allocations.assigned_service_id — bind pool IP to a VM service.

Revision ID: vm_ip_alloc_svc
Revises: drop_vm_tpl_os_prof
Create Date: 2026-03-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "vm_ip_alloc_svc"
down_revision: Union[str, Sequence[str], None] = "drop_vm_tpl_os_prof"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vm_ip_allocations",
        sa.Column("assigned_service_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_vm_ip_allocations_assigned_service_id"),
        "vm_ip_allocations",
        ["assigned_service_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_vm_ip_allocations_assigned_service_id_services"),
        "vm_ip_allocations",
        "services",
        ["assigned_service_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_vm_ip_allocations_assigned_service_id_services"),
        "vm_ip_allocations",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_vm_ip_allocations_assigned_service_id"), table_name="vm_ip_allocations")
    op.drop_column("vm_ip_allocations", "assigned_service_id")
