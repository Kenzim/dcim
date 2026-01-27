"""remove plugin tables and use plugin_name strings

Revision ID: remove_plugin_tables_and_use_plugin_name
Revises: rename_plugins_to_server_plugins
Create Date: 2026-01-23 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'remove_plugin_tables'
down_revision: Union[str, Sequence[str], None] = 'rename_plugins_to_server_plugins'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove plugin tables and update servers/switches to use plugin_name strings"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    # Update servers table: change plugin_id FK to plugin_name string
    if 'servers' in tables:
        # Find and drop foreign key constraint
        # Query information_schema to find all foreign key constraints on plugin_id
        fk_query = sa.text("""
            SELECT CONSTRAINT_NAME 
            FROM information_schema.KEY_COLUMN_USAGE 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'servers' 
            AND COLUMN_NAME = 'plugin_id' 
            AND REFERENCED_TABLE_NAME IS NOT NULL
        """)
        result = conn.execute(fk_query)
        fk_names = [row[0] for row in result.fetchall()]
        
        # Drop all foreign key constraints found using raw SQL (more reliable for MySQL)
        for fk_name in fk_names:
            try:
                conn.execute(sa.text(f"ALTER TABLE servers DROP FOREIGN KEY {fk_name}"))
            except Exception as e:
                pass  # Constraint might already be dropped
        
        # Also try common MySQL constraint names as fallback
        for constraint_name in ['servers_plugin_id_fkey', 'servers_ibfk_2', 'servers_ibfk_1', 'servers_ibfk_3']:
            try:
                conn.execute(sa.text(f"ALTER TABLE servers DROP FOREIGN KEY {constraint_name}"))
            except:
                pass  # Ignore if doesn't exist
        
        # Drop the index if it exists (MySQL may keep it after FK drop)
        indexes = [idx['name'] for idx in inspector.get_indexes('servers')]
        if 'ix_servers_plugin_id' in indexes:
            try:
                conn.execute(sa.text("ALTER TABLE servers DROP INDEX ix_servers_plugin_id"))
            except:
                pass  # Index might already be dropped
        
        # Check if plugin_name column already exists
        columns = [col['name'] for col in inspector.get_columns('servers')]
        plugin_name_exists = 'plugin_name' in columns
        plugin_id_exists = 'plugin_id' in columns
        
        # Get plugin names from server_plugins table before dropping it
        # We'll need to migrate the data
        if 'server_plugins' in tables and plugin_id_exists:
            # Get mapping of plugin_id -> plugin_name
            plugin_mapping = {}
            result = conn.execute(sa.text("SELECT id, name FROM server_plugins"))
            for row in result:
                plugin_mapping[row[0]] = row[1]
            
            # Add plugin_name column if it doesn't exist
            if not plugin_name_exists:
                op.add_column('servers', sa.Column('plugin_name', sa.String(255), nullable=True))
            
            # Migrate data: set plugin_name based on plugin_id (only if plugin_id exists)
            if plugin_id_exists:
                for plugin_id, plugin_name in plugin_mapping.items():
                    conn.execute(
                        sa.text("UPDATE servers SET plugin_name = :name WHERE plugin_id = :id"),
                        {"name": plugin_name, "id": plugin_id}
                    )
            
            # Drop plugin_id column if it exists
            if plugin_id_exists:
                op.drop_column('servers', 'plugin_id')
            
            # Make plugin_name NOT NULL and add index if column exists
            if plugin_name_exists:
                # Check if index already exists
                indexes = [idx['name'] for idx in inspector.get_indexes('servers')]
                if 'ix_servers_plugin_name' not in indexes:
                    op.create_index(op.f('ix_servers_plugin_name'), 'servers', ['plugin_name'], unique=False)
                # Make sure it's NOT NULL (MySQL requires existing type)
                op.alter_column('servers', 'plugin_name', 
                               existing_type=sa.String(255),
                               nullable=False)
        elif not plugin_name_exists:
            # If server_plugins table doesn't exist and plugin_name doesn't exist, add it
            op.add_column('servers', sa.Column('plugin_name', sa.String(255), nullable=False))
            op.create_index(op.f('ix_servers_plugin_name'), 'servers', ['plugin_name'], unique=False)
    
    # Update network_switches table: change plugin_id FK to plugin_name string
    if 'network_switches' in tables:
        # Find and drop foreign key constraint
        # Query information_schema to find all foreign key constraints on plugin_id
        fk_query = sa.text("""
            SELECT CONSTRAINT_NAME 
            FROM information_schema.KEY_COLUMN_USAGE 
            WHERE TABLE_SCHEMA = DATABASE() 
            AND TABLE_NAME = 'network_switches' 
            AND COLUMN_NAME = 'plugin_id' 
            AND REFERENCED_TABLE_NAME IS NOT NULL
        """)
        result = conn.execute(fk_query)
        fk_names = [row[0] for row in result.fetchall()]
        
        # Drop all foreign key constraints found using raw SQL (more reliable for MySQL)
        for fk_name in fk_names:
            try:
                conn.execute(sa.text(f"ALTER TABLE network_switches DROP FOREIGN KEY {fk_name}"))
            except Exception as e:
                pass  # Constraint might already be dropped
        
        # Also try common MySQL constraint names as fallback
        for constraint_name in ['network_switches_plugin_id_fkey', 'network_switches_ibfk_1', 'network_switches_ibfk_2', 'network_switches_ibfk_3']:
            try:
                conn.execute(sa.text(f"ALTER TABLE network_switches DROP FOREIGN KEY {constraint_name}"))
            except:
                pass  # Ignore if doesn't exist
        
        # Drop the index if it exists (MySQL may keep it after FK drop)
        indexes = [idx['name'] for idx in inspector.get_indexes('network_switches')]
        if 'ix_network_switches_plugin_id' in indexes:
            try:
                conn.execute(sa.text("ALTER TABLE network_switches DROP INDEX ix_network_switches_plugin_id"))
            except:
                pass  # Index might already be dropped
        
        # Check if plugin_name column already exists
        columns = [col['name'] for col in inspector.get_columns('network_switches')]
        plugin_name_exists = 'plugin_name' in columns
        plugin_id_exists = 'plugin_id' in columns
        
        # Get plugin names from switch_plugins table before dropping it
        if 'switch_plugins' in tables and plugin_id_exists:
            # Get mapping of plugin_id -> plugin_name
            plugin_mapping = {}
            result = conn.execute(sa.text("SELECT id, name FROM switch_plugins"))
            for row in result:
                plugin_mapping[row[0]] = row[1]
            
            # Add plugin_name column if it doesn't exist
            if not plugin_name_exists:
                op.add_column('network_switches', sa.Column('plugin_name', sa.String(255), nullable=True))
            
            # Migrate data: set plugin_name based on plugin_id (only if plugin_id exists)
            if plugin_id_exists:
                for plugin_id, plugin_name in plugin_mapping.items():
                    conn.execute(
                        sa.text("UPDATE network_switches SET plugin_name = :name WHERE plugin_id = :id"),
                        {"name": plugin_name, "id": plugin_id}
                    )
            
            # Drop plugin_id column if it exists
            if plugin_id_exists:
                op.drop_column('network_switches', 'plugin_id')
            
            # Make plugin_name NOT NULL and add index if column exists
            if plugin_name_exists:
                # Check if index already exists
                indexes = [idx['name'] for idx in inspector.get_indexes('network_switches')]
                if 'ix_network_switches_plugin_name' not in indexes:
                    op.create_index(op.f('ix_network_switches_plugin_name'), 'network_switches', ['plugin_name'], unique=False)
                # Make sure it's NOT NULL (MySQL requires existing type)
                op.alter_column('network_switches', 'plugin_name',
                               existing_type=sa.String(255),
                               nullable=False)
        elif not plugin_name_exists:
            # If switch_plugins table doesn't exist and plugin_name doesn't exist, add it
            op.add_column('network_switches', sa.Column('plugin_name', sa.String(255), nullable=False))
            op.create_index(op.f('ix_network_switches_plugin_name'), 'network_switches', ['plugin_name'], unique=False)
    
    # Drop plugin tables and related tables
    if 'plugin_categories' in tables:
        op.drop_table('plugin_categories')
    
    if 'server_plugins' in tables:
        op.drop_table('server_plugins')
    
    if 'switch_plugins' in tables:
        op.drop_table('switch_plugins')


def downgrade() -> None:
    """Revert: recreate plugin tables and restore plugin_id FKs"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    # Recreate server_plugins table
    if 'server_plugins' not in tables:
        op.create_table('server_plugins',
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
        op.create_index(op.f('ix_server_plugins_id'), 'server_plugins', ['id'], unique=False)
        op.create_index(op.f('ix_server_plugins_name'), 'server_plugins', ['name'], unique=True)
    
    # Recreate switch_plugins table
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
    
    # Recreate plugin_categories junction table
    if 'plugin_categories' not in tables:
        op.create_table('plugin_categories',
            sa.Column('plugin_id', sa.Integer(), nullable=False),
            sa.Column('category_id', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['plugin_id'], ['server_plugins.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('plugin_id', 'category_id')
        )
    
    # Revert servers table
    if 'servers' in tables:
        # Add plugin_id column
        op.add_column('servers', sa.Column('plugin_id', sa.Integer(), nullable=True))
        
        # Note: We can't restore the plugin_id values without knowing which plugin_name maps to which ID
        # This would require manual data migration
        
        # Drop plugin_name column and index
        op.drop_index(op.f('ix_servers_plugin_name'), table_name='servers')
        op.drop_column('servers', 'plugin_name')
        
        # Recreate foreign key (but plugin_id will be NULL, needs manual fix)
        op.create_foreign_key(
            'servers_plugin_id_fkey',
            'servers', 'server_plugins',
            ['plugin_id'], ['id']
        )
    
    # Revert network_switches table
    if 'network_switches' in tables:
        # Add plugin_id column
        op.add_column('network_switches', sa.Column('plugin_id', sa.Integer(), nullable=True))
        
        # Note: We can't restore the plugin_id values without knowing which plugin_name maps to which ID
        # This would require manual data migration
        
        # Drop plugin_name column and index
        op.drop_index(op.f('ix_network_switches_plugin_name'), table_name='network_switches')
        op.drop_column('network_switches', 'plugin_name')
        
        # Recreate foreign key (but plugin_id will be NULL, needs manual fix)
        op.create_foreign_key(
            'network_switches_plugin_id_fkey',
            'network_switches', 'switch_plugins',
            ['plugin_id'], ['id']
        )
