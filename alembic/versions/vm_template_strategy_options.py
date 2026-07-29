"""Add strategy_options JSON to vm_templates.

Revision ID: vm_tpl_strategy_opts
Revises: add_vm_deploy_jobs
Create Date: 2026-07-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON


revision: str = "vm_tpl_strategy_opts"
down_revision: Union[str, None] = "add_vm_deploy_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vm_templates",
        sa.Column("strategy_options", JSON, nullable=True),
    )
    op.execute("UPDATE vm_templates SET strategy_options = JSON_OBJECT()")
    op.alter_column(
        "vm_templates",
        "strategy_options",
        existing_type=JSON,
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("vm_templates", "strategy_options")
