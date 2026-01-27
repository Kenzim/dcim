"""merge_plugin_removal_and_switch_ports

Revision ID: merge_heads_001
Revises: remove_plugin_tables, add_switch_ports_and_cable_runs
Create Date: 2026-01-23 13:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'merge_heads_001'
down_revision: Union[str, Sequence[str], None] = ('remove_plugin_tables', 'add_switch_ports_and_cable_runs')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge migration - no changes needed, just merges the two branches"""
    pass


def downgrade() -> None:
    """Merge migration - no changes needed"""
    pass
