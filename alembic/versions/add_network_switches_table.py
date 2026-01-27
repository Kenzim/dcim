"""add_network_switches_table

Revision ID: add_network_switches_table
Revises: 9530a1d7a463
Create Date: 2026-01-23 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_network_switches_table'
down_revision: Union[str, Sequence[str], None] = '9530a1d7a463'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add network_switches table"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    # Create network_switches table if it doesn't exist
    if 'network_switches' not in tables:
        op.create_table('network_switches',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=255), nullable=False),
            sa.Column('management_ip', sa.String(length=45), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('location_id', sa.Integer(), nullable=False),
            sa.Column('rack_id', sa.Integer(), nullable=True),
            sa.Column('rack_unit', sa.Integer(), nullable=True),
            sa.Column('plugin_id', sa.Integer(), nullable=False),
            sa.Column('plugin_config', sa.JSON(), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1'),
            sa.Column('port_count', sa.Integer(), nullable=True),
            sa.Column('model', sa.String(length=255), nullable=True),
            sa.Column('serial_number', sa.String(length=255), nullable=True),
            sa.Column('firmware_version', sa.String(length=255), nullable=True),
            sa.Column('tested_capabilities', sa.JSON(), nullable=True),
            sa.Column('test_logs', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['location_id'], ['locations.id'], ),
            sa.ForeignKeyConstraint(['rack_id'], ['racks.id'], ),
            sa.ForeignKeyConstraint(['plugin_id'], ['plugins.id'], ),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_network_switches_id'), 'network_switches', ['id'], unique=False)
        op.create_index(op.f('ix_network_switches_name'), 'network_switches', ['name'], unique=True)
        op.create_index(op.f('ix_network_switches_location_id'), 'network_switches', ['location_id'], unique=False)
        op.create_index(op.f('ix_network_switches_rack_id'), 'network_switches', ['rack_id'], unique=False)
        op.create_index(op.f('ix_network_switches_plugin_id'), 'network_switches', ['plugin_id'], unique=False)


def downgrade() -> None:
    """Remove network_switches table"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'network_switches' in inspector.get_table_names():
        op.drop_index(op.f('ix_network_switches_plugin_id'), table_name='network_switches')
        op.drop_index(op.f('ix_network_switches_rack_id'), table_name='network_switches')
        op.drop_index(op.f('ix_network_switches_location_id'), table_name='network_switches')
        op.drop_index(op.f('ix_network_switches_name'), table_name='network_switches')
        op.drop_index(op.f('ix_network_switches_id'), table_name='network_switches')
        op.drop_table('network_switches')
