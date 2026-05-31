"""add_server_pxe_kernel_args

Revision ID: add_server_pxe_kernel_args
Revises: proxmox_bytes_bigint
Create Date: 2026-03-27 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "add_server_pxe_kernel_args"
down_revision: Union[str, Sequence[str], None] = "proxmox_bytes_bigint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("pxe_kernel_args_general", sa.Text(), nullable=True))
    op.add_column("servers", sa.Column("pxe_kernel_args_network", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "pxe_kernel_args_network")
    op.drop_column("servers", "pxe_kernel_args_general")
