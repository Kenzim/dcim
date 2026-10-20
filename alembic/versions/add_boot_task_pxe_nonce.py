"""add per-task PXE boot nonce to boot_tasks

Revision ID: add_boot_task_pxe_nonce
Revises: add_runners_table
Create Date: 2026-10-14 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_boot_task_pxe_nonce"
down_revision: Union[str, Sequence[str], None] = "add_runners_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table_name: str) -> set:
    return {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = _columns(inspector, "boot_tasks")
    if "pxe_nonce" not in existing:
        op.add_column("boot_tasks", sa.Column("pxe_nonce", sa.String(64), nullable=True))
    if "pxe_nonce_used_at" not in existing:
        op.add_column("boot_tasks", sa.Column("pxe_nonce_used_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = _columns(inspector, "boot_tasks")
    if "pxe_nonce_used_at" in existing:
        op.drop_column("boot_tasks", "pxe_nonce_used_at")
    if "pxe_nonce" in existing:
        op.drop_column("boot_tasks", "pxe_nonce")
