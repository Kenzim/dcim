"""add_racks_table

Revision ID: add_racks_table
Revises: add_ipmi_proxy_to_servers
Create Date: 2026-01-23 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_racks_table'
down_revision: Union[str, Sequence[str], None] = 'add_ipmi_proxy_to_servers'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add racks table and rack fields to servers table"""
    # Check if racks table already exists
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    # Create racks table if it doesn't exist
    if 'racks' not in tables:
        op.create_table('racks',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('location_id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=255), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('units', sa.Integer(), nullable=False, server_default='42'),
            sa.Column('row', sa.Integer(), nullable=True),
            sa.Column('row_position', sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(['location_id'], ['locations.id'], ),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_racks_id'), 'racks', ['id'], unique=False)
        op.create_index(op.f('ix_racks_location_id'), 'racks', ['location_id'], unique=False)
        op.create_index(op.f('ix_racks_name'), 'racks', ['name'], unique=False)
        op.create_index(op.f('ix_racks_row'), 'racks', ['row'], unique=False)
    else:
        # Add row fields to existing racks table if they don't exist
        columns = {col['name']: col for col in inspector.get_columns('racks')}
        if 'row' not in columns:
            op.add_column('racks', sa.Column('row', sa.Integer(), nullable=True))
            op.create_index(op.f('ix_racks_row'), 'racks', ['row'], unique=False)
        if 'row_position' not in columns:
            op.add_column('racks', sa.Column('row_position', sa.Integer(), nullable=True))
    
    # Add rack fields to servers table if they don't exist
    if 'servers' in tables:
        columns = {col['name']: col for col in inspector.get_columns('servers')}
        
        if 'rack_id' not in columns:
            op.add_column('servers', sa.Column('rack_id', sa.Integer(), nullable=True))
            op.create_foreign_key('fk_servers_rack', 'servers', 'racks', ['rack_id'], ['id'])
            op.create_index(op.f('ix_servers_rack_id'), 'servers', ['rack_id'], unique=False)
        
        if 'rack_unit' not in columns:
            op.add_column('servers', sa.Column('rack_unit', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove racks table and rack fields from servers table"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    # Remove rack fields from servers table
    if 'servers' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('servers')}
        
        if 'rack_unit' in columns:
            op.drop_column('servers', 'rack_unit')
        
        if 'rack_id' in columns:
            try:
                op.drop_index(op.f('ix_servers_rack_id'), table_name='servers')
            except Exception:
                pass
            try:
                op.drop_constraint('fk_servers_rack', 'servers', type_='foreignkey')
            except Exception:
                pass
            op.drop_column('servers', 'rack_id')
    
    # Drop racks table
    if 'racks' in inspector.get_table_names():
        try:
            op.drop_index(op.f('ix_racks_row'), table_name='racks')
        except Exception:
            pass
        op.drop_index(op.f('ix_racks_name'), table_name='racks')
        op.drop_index(op.f('ix_racks_location_id'), table_name='racks')
        op.drop_index(op.f('ix_racks_id'), table_name='racks')
        op.drop_table('racks')
