"""add locations, disks, and server fields

Revision ID: add_locations_disks
Revises: add_categories
Create Date: 2026-01-14 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_locations_disks'
down_revision: Union[str, Sequence[str], None] = 'add_categories'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Check if locations table exists
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    # Create locations table if it doesn't exist
    if 'locations' not in tables:
        op.create_table('locations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_locations_id'), 'locations', ['id'], unique=False)
        op.create_index(op.f('ix_locations_name'), 'locations', ['name'], unique=True)
    
    # Check if servers table has 'name' column (already migrated) or 'hostname' (needs migration)
    if 'servers' in tables:
        columns = [col['name'] for col in inspector.get_columns('servers')]
        if 'hostname' in columns and 'name' not in columns:
            # Rename hostname to name for consistency
            # MySQL requires existing_type when renaming columns
            op.alter_column('servers', 'hostname', new_column_name='name', existing_type=sa.String(255))
    
    # Add new server fields (only if they don't exist)
    if 'servers' in tables:
        columns = [col['name'] for col in inspector.get_columns('servers')]
        if 'server_ip' not in columns:
            op.add_column('servers', sa.Column('server_ip', sa.String(length=45), nullable=True))
        if 'description' not in columns:
            op.add_column('servers', sa.Column('description', sa.Text(), nullable=True))
        if 'cpu_count' not in columns:
            op.add_column('servers', sa.Column('cpu_count', sa.Integer(), nullable=True, server_default='1'))
        if 'cpu_model' not in columns:
            op.add_column('servers', sa.Column('cpu_model', sa.String(length=255), nullable=True))
        if 'ram_gb' not in columns:
            op.add_column('servers', sa.Column('ram_gb', sa.Integer(), nullable=True))
        if 'port_speed' not in columns:
            op.add_column('servers', sa.Column('port_speed', sa.String(length=50), nullable=True))
        if 'location_id' not in columns:
            op.add_column('servers', sa.Column('location_id', sa.Integer(), nullable=True))
    
    # Create foreign key for location (if it doesn't exist)
    if 'servers' in tables:
        columns = [col['name'] for col in inspector.get_columns('servers')]
        if 'location_id' in columns:
            # Check if foreign key already exists
            fks = [fk['name'] for fk in inspector.get_foreign_keys('servers')]
            if 'fk_servers_location' not in fks:
                op.create_foreign_key('fk_servers_location', 'servers', 'locations', ['location_id'], ['id'])
            # Check if index exists
            indexes = [idx['name'] for idx in inspector.get_indexes('servers')]
            if 'ix_servers_location_id' not in indexes:
                op.create_index(op.f('ix_servers_location_id'), 'servers', ['location_id'], unique=False)
    
    # Set default values for existing rows (only if columns exist)
    if 'servers' in tables:
        columns = [col['name'] for col in inspector.get_columns('servers')]
        if 'server_ip' in columns:
            op.execute("UPDATE servers SET server_ip = '0.0.0.0' WHERE server_ip IS NULL")
        if 'cpu_count' in columns:
            op.execute("UPDATE servers SET cpu_count = 1 WHERE cpu_count IS NULL")
        
        # Make server_ip and cpu_count NOT NULL after setting defaults
        if 'server_ip' in columns:
            # Check current nullable status
            for col in inspector.get_columns('servers'):
                if col['name'] == 'server_ip' and col['nullable']:
                    op.alter_column('servers', 'server_ip', nullable=False, existing_type=sa.String(45))
        if 'cpu_count' in columns:
            for col in inspector.get_columns('servers'):
                if col['name'] == 'cpu_count' and col['nullable']:
                    op.alter_column('servers', 'cpu_count', nullable=False, existing_type=sa.Integer())
    
    # Create disks table if it doesn't exist
    if 'disks' not in tables:
        op.create_table('disks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.Enum('ssd', 'hdd', name='disktype'), nullable=False),
        sa.Column('capacity_gb', sa.Integer(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_disks_id'), 'disks', ['id'], unique=False)
        op.create_index(op.f('ix_disks_server_id'), 'disks', ['server_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Drop disks table
    op.drop_index(op.f('ix_disks_server_id'), table_name='disks')
    op.drop_index(op.f('ix_disks_id'), table_name='disks')
    op.drop_table('disks')
    
    # Remove server fields
    op.drop_index(op.f('ix_servers_location_id'), table_name='servers')
    op.drop_constraint('fk_servers_location', 'servers', type_='foreignkey')
    op.drop_column('servers', 'location_id')
    op.drop_column('servers', 'port_speed')
    op.drop_column('servers', 'ram_gb')
    op.drop_column('servers', 'cpu_model')
    op.drop_column('servers', 'cpu_count')
    op.drop_column('servers', 'description')
    op.drop_column('servers', 'server_ip')
    
    # Rename name back to hostname
    op.alter_column('servers', 'name', new_column_name='hostname', existing_type=sa.String(255))
    
    # Drop locations table
    op.drop_index(op.f('ix_locations_name'), table_name='locations')
    op.drop_index(op.f('ix_locations_id'), table_name='locations')
    op.drop_table('locations')

