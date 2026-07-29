"""vm_ip_allocations.batch_tag — group IPs added together for filtering.

Revision ID: vm_ip_alloc_tag
Revises: client_permission_sets
Create Date: 2026-07-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "vm_ip_alloc_tag"
down_revision: Union[str, Sequence[str], None] = "client_permission_sets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vm_ip_allocations",
        sa.Column("batch_tag", sa.String(length=100), nullable=True),
    )
    op.create_index(
        op.f("ix_vm_ip_allocations_batch_tag"),
        "vm_ip_allocations",
        ["batch_tag"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_vm_ip_allocations_batch_tag"), table_name="vm_ip_allocations")
    op.drop_column("vm_ip_allocations", "batch_tag")
