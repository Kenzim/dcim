"""Add permitted options to server_groups for WHMCS/billing

Revision ID: add_srvgrp_permitted
Revises: merge_bandwidth_cable
Create Date: 2026-02-20

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "add_srvgrp_permitted"
down_revision: Union[str, Sequence[str], None] = "merge_bandwidth_cable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("server_groups", sa.Column("enable_isos", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("server_groups", sa.Column("permitted_isos", sa.JSON(), nullable=True))
    op.add_column("server_groups", sa.Column("enable_temp_os", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("server_groups", sa.Column("permitted_temp_os", sa.JSON(), nullable=True))
    op.add_column("server_groups", sa.Column("enable_scripts", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("server_groups", sa.Column("permitted_scripts", sa.JSON(), nullable=True))
    op.add_column("server_groups", sa.Column("enable_os_templates", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("server_groups", sa.Column("permitted_os_templates", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_groups", "permitted_os_templates")
    op.drop_column("server_groups", "enable_os_templates")
    op.drop_column("server_groups", "permitted_scripts")
    op.drop_column("server_groups", "enable_scripts")
    op.drop_column("server_groups", "permitted_temp_os")
    op.drop_column("server_groups", "enable_temp_os")
    op.drop_column("server_groups", "permitted_isos")
    op.drop_column("server_groups", "enable_isos")
