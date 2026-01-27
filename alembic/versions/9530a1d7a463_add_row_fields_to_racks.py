"""add_row_fields_to_racks

Revision ID: 9530a1d7a463
Revises: add_racks_table
Create Date: 2026-01-23 21:44:41.973946

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9530a1d7a463'
down_revision: Union[str, Sequence[str], None] = 'add_racks_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add row and row_position columns to racks table"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'racks' not in inspector.get_table_names():
        return
    
    columns = {col['name']: col for col in inspector.get_columns('racks')}
    
    # Add row column if it doesn't exist
    if 'row' not in columns:
        op.add_column('racks', sa.Column('row', sa.Integer(), nullable=True))
        op.create_index(op.f('ix_racks_row'), 'racks', ['row'], unique=False)
    
    # Add row_position column if it doesn't exist
    if 'row_position' not in columns:
        op.add_column('racks', sa.Column('row_position', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove row and row_position columns from racks table"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'racks' not in inspector.get_table_names():
        return
    
    columns = {col['name']: col for col in inspector.get_columns('racks')}
    
    # Remove row_position column if it exists
    if 'row_position' in columns:
        op.drop_column('racks', 'row_position')
    
    # Remove row column and index if they exist
    if 'row' in columns:
        try:
            op.drop_index(op.f('ix_racks_row'), table_name='racks')
        except Exception:
            pass
        op.drop_column('racks', 'row')
