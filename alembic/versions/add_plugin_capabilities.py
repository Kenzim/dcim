"""add plugin capabilities fields

Revision ID: add_plugin_capabilities
Revises: make_location_required
Create Date: 2026-01-16 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_plugin_capabilities'
down_revision: Union[str, Sequence[str], None] = 'make_location_required'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'plugins' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('plugins')}
        
        # Add available_capabilities JSON column
        if 'available_capabilities' not in columns:
            op.add_column('plugins', sa.Column('available_capabilities', sa.JSON(), nullable=True))
        
        # Add tested_capabilities JSON column
        if 'tested_capabilities' not in columns:
            op.add_column('plugins', sa.Column('tested_capabilities', sa.JSON(), nullable=True))
        
        # Add test_logs TEXT column
        if 'test_logs' not in columns:
            op.add_column('plugins', sa.Column('test_logs', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'plugins' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('plugins')}
        
        # Remove test_logs column
        if 'test_logs' in columns:
            op.drop_column('plugins', 'test_logs')
        
        # Remove tested_capabilities column
        if 'tested_capabilities' in columns:
            op.drop_column('plugins', 'tested_capabilities')
        
        # Remove available_capabilities column
        if 'available_capabilities' in columns:
            op.drop_column('plugins', 'available_capabilities')

