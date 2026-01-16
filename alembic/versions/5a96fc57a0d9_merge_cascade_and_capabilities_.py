"""merge cascade and capabilities migrations

Revision ID: 5a96fc57a0d9
Revises: add_cascade_plugin_categories, move_capabilities_to_servers
Create Date: 2026-01-16 20:04:43.846249

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5a96fc57a0d9'
down_revision: Union[str, Sequence[str], None] = ('add_cascade_plugin_categories', 'move_capabilities_to_servers')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
