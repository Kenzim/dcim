"""rename plugins table to server_plugins

Revision ID: rename_plugins_to_server_plugins
Revises: add_switch_plugins_table
Create Date: 2026-01-23 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'rename_plugins_to_server_plugins'
down_revision: Union[str, Sequence[str], None] = 'add_switch_plugins_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename plugins table to server_plugins and update foreign keys"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    # Only proceed if plugins table exists
    if 'plugins' not in tables:
        return
    
    # Rename the table
    op.rename_table('plugins', 'server_plugins')
    
    # Update foreign key in plugin_categories table
    if 'plugin_categories' in tables:
        # Drop existing foreign key constraint
        op.drop_constraint('plugin_categories_ibfk_1', 'plugin_categories', type_='foreignkey')
        # Recreate with new table name
        op.create_foreign_key(
            'plugin_categories_ibfk_1',
            'plugin_categories', 'server_plugins',
            ['plugin_id'], ['id'],
            ondelete='CASCADE'
        )
    
    # Update foreign key in servers table
    if 'servers' in tables:
        # Get the constraint name (may vary by database)
        # For MySQL, it's typically servers_ibfk_X
        # For PostgreSQL, it's typically servers_plugin_id_fkey
        try:
            # Try to drop by common MySQL constraint name
            op.drop_constraint('servers_ibfk_2', 'servers', type_='foreignkey')
        except:
            # If that fails, try PostgreSQL style
            try:
                op.drop_constraint('servers_plugin_id_fkey', 'servers', type_='foreignkey')
            except:
                # If both fail, find the constraint dynamically
                pass
        
        # Recreate with new table name
        op.create_foreign_key(
            'servers_plugin_id_fkey',
            'servers', 'server_plugins',
            ['plugin_id'], ['id']
        )


def downgrade() -> None:
    """Revert server_plugins table back to plugins"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    # Only proceed if server_plugins table exists
    if 'server_plugins' not in tables:
        return
    
    # Update foreign key in servers table first
    if 'servers' in tables:
        try:
            op.drop_constraint('servers_plugin_id_fkey', 'servers', type_='foreignkey')
        except:
            pass
        op.create_foreign_key(
            'servers_plugin_id_fkey',
            'servers', 'plugins',
            ['plugin_id'], ['id']
        )
    
    # Update foreign key in plugin_categories table
    if 'plugin_categories' in tables:
        op.drop_constraint('plugin_categories_ibfk_1', 'plugin_categories', type_='foreignkey')
        op.create_foreign_key(
            'plugin_categories_ibfk_1',
            'plugin_categories', 'plugins',
            ['plugin_id'], ['id'],
            ondelete='CASCADE'
        )
    
    # Rename the table back
    op.rename_table('server_plugins', 'plugins')
