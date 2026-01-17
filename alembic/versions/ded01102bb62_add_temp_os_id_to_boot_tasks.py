"""add_temp_os_id_to_boot_tasks

Revision ID: ded01102bb62
Revises: add_alpine_boot_type
Create Date: 2026-01-17 14:42:18.462327

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ded01102bb62'
down_revision: Union[str, Sequence[str], None] = 'add_alpine_boot_type'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add temp_os_id column to boot_tasks table."""
    op.add_column('boot_tasks', sa.Column('temp_os_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Remove temp_os_id column from boot_tasks table."""
    op.drop_column('boot_tasks', 'temp_os_id')
