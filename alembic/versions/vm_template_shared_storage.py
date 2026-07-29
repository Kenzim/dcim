"""Add shared_storage flag to vm_templates.

Revision ID: vm_tpl_shared_storage
Revises: reseller_platform_phase11_payment_method_type
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "vm_tpl_shared_storage"
down_revision: Union[str, None] = "reseller_platform_phase11_payment_method_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("vm_templates")}
    if "shared_storage" in columns:
        return

    op.add_column(
        "vm_templates",
        sa.Column(
            "shared_storage",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("vm_templates")}
    if "shared_storage" not in columns:
        return
    op.drop_column("vm_templates", "shared_storage")
