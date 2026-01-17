"""add_boot_mode_to_servers

Revision ID: e4a3f01f2b09
Revises: b78a58f6b036
Create Date: 2026-01-17 16:31:26.822830

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4a3f01f2b09'
down_revision: Union[str, Sequence[str], None] = 'b78a58f6b036'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add boot_mode column to servers table."""
    # Add boot_mode column with default value 'uefi'
    op.add_column('servers', 
        sa.Column('boot_mode', sa.String(10), nullable=False, server_default='uefi')
    )


def downgrade() -> None:
    """Remove boot_mode column from servers table."""
    op.drop_column('servers', 'boot_mode')
