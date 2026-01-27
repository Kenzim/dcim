"""add_switch_plugins_table

Revision ID: add_switch_plugins_table
Revises: add_network_switches_table
Create Date: 2026-01-23 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_switch_plugins_table'
down_revision: Union[str, Sequence[str], None] = 'add_network_switches_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add switch_plugins table and update network_switches foreign key"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    # Create switch_plugins table if it doesn't exist
    if 'switch_plugins' not in tables:
        op.create_table('switch_plugins',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=255), nullable=False),
            sa.Column('version', sa.String(length=50), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('config_template', sa.JSON(), nullable=False),
            sa.Column('available_capabilities', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_switch_plugins_id'), 'switch_plugins', ['id'], unique=False)
        op.create_index(op.f('ix_switch_plugins_name'), 'switch_plugins', ['name'], unique=True)
    
    # Update network_switches table to reference switch_plugins instead of plugins
    if 'network_switches' in tables:
        columns = {col['name']: col for col in inspector.get_columns('network_switches')}
        foreign_keys = inspector.get_foreign_keys('network_switches')
        
        # Check if foreign key still points to plugins table
        fk_to_plugins = None
        for fk in foreign_keys:
            if fk['constrained_columns'] == ['plugin_id'] and fk['referred_table'] == 'plugins':
                fk_to_plugins = fk
                break
        
        if fk_to_plugins:
            # Drop old foreign key constraint
            op.drop_constraint(fk_to_plugins['name'], 'network_switches', type_='foreignkey')
            
            # Add new foreign key constraint to switch_plugins
            op.create_foreign_key(
                'fk_network_switches_plugin_id_switch_plugins',
                'network_switches', 'switch_plugins',
                ['plugin_id'], ['id']
            )


def downgrade() -> None:
    """Revert changes - update foreign key back to plugins and drop switch_plugins table"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    # Update network_switches foreign key back to plugins
    if 'network_switches' in inspector.get_table_names():
        foreign_keys = inspector.get_foreign_keys('network_switches')
        
        # Check if foreign key points to switch_plugins
        fk_to_switch_plugins = None
        for fk in foreign_keys:
            if fk['constrained_columns'] == ['plugin_id'] and fk['referred_table'] == 'switch_plugins':
                fk_to_switch_plugins = fk
                break
        
        if fk_to_switch_plugins:
            # Drop foreign key to switch_plugins
            op.drop_constraint(fk_to_switch_plugins['name'], 'network_switches', type_='foreignkey')
            
            # Add foreign key back to plugins (if plugins table exists)
            if 'plugins' in inspector.get_table_names():
                op.create_foreign_key(
                    'fk_network_switches_plugin_id_plugins',
                    'network_switches', 'plugins',
                    ['plugin_id'], ['id']
                )
    
    # Drop switch_plugins table
    if 'switch_plugins' in inspector.get_table_names():
        op.drop_index(op.f('ix_switch_plugins_name'), table_name='switch_plugins')
        op.drop_index(op.f('ix_switch_plugins_id'), table_name='switch_plugins')
        op.drop_table('switch_plugins')
