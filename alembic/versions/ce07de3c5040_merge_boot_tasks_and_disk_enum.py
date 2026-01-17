"""merge_boot_tasks_and_disk_enum

Revision ID: ce07de3c5040
Revises: add_iso_install_tasks, fix_disk_enum
Create Date: 2026-01-17 13:33:48.132119

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ce07de3c5040'
down_revision: Union[str, Sequence[str], None] = ('add_iso_install_tasks', 'fix_disk_enum')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
