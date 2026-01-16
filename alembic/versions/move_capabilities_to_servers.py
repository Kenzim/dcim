"""move tested capabilities from plugins to servers

Revision ID: move_capabilities_to_servers
Revises: add_plugin_capabilities
Create Date: 2026-01-16 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'move_capabilities_to_servers'
down_revision: Union[str, Sequence[str], None] = 'add_plugin_capabilities'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    # Add tested_capabilities and test_logs to servers table
    if 'servers' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('servers')}
        
        # Add tested_capabilities JSON column
        if 'tested_capabilities' not in columns:
            op.add_column('servers', sa.Column('tested_capabilities', sa.JSON(), nullable=True))
        
        # Add test_logs TEXT column
        if 'test_logs' not in columns:
            op.add_column('servers', sa.Column('test_logs', sa.Text(), nullable=True))
    
    # Remove tested_capabilities and test_logs from plugins table
    if 'plugins' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('plugins')}
        
        # Remove test_logs column
        if 'test_logs' in columns:
            op.drop_column('plugins', 'test_logs')
        
        # Remove tested_capabilities column
        if 'tested_capabilities' in columns:
            op.drop_column('plugins', 'tested_capabilities')


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    # Add tested_capabilities and test_logs back to plugins table
    if 'plugins' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('plugins')}
        
        # Add tested_capabilities JSON column
        if 'tested_capabilities' not in columns:
            op.add_column('plugins', sa.Column('tested_capabilities', sa.JSON(), nullable=True))
        
        # Add test_logs TEXT column
        if 'test_logs' not in columns:
            op.add_column('plugins', sa.Column('test_logs', sa.Text(), nullable=True))
    
    # Remove tested_capabilities and test_logs from servers table
    if 'servers' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('servers')}
        
        # Remove test_logs column
        if 'test_logs' in columns:
            op.drop_column('servers', 'test_logs')
        
        # Remove tested_capabilities column
        if 'tested_capabilities' in columns:
            op.drop_column('servers', 'tested_capabilities')

