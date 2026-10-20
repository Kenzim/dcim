"""add boot_method (pxe | vmedia) to boot_tasks

Revision ID: add_boot_task_boot_method
Revises: add_boot_task_pxe_nonce
Create Date: 2026-10-14 13:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_boot_task_boot_method"
down_revision: Union[str, Sequence[str], None] = "add_boot_task_pxe_nonce"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table_name: str) -> set:
    return {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "boot_method" not in _columns(inspector, "boot_tasks"):
        op.add_column(
            "boot_tasks",
            sa.Column("boot_method", sa.String(16), nullable=False, server_default="pxe"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "boot_method" in _columns(inspector, "boot_tasks"):
        op.drop_column("boot_tasks", "boot_method")
