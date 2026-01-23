"""add_credentials_to_servers

Revision ID: add_server_credentials
Revises: add_os_disk_serial
Create Date: 2026-01-18 03:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_server_credentials'
down_revision: Union[str, Sequence[str], None] = 'add_os_disk_serial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add credentials column to servers table"""
    # Check if servers table exists
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'servers' not in inspector.get_table_names():
        return  # Table doesn't exist yet
    
    columns = {col['name']: col for col in inspector.get_columns('servers')}
    
    # Add credentials column if it doesn't exist
    if 'credentials' not in columns:
        op.add_column('servers', sa.Column('credentials', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Remove credentials column from servers table"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'servers' not in inspector.get_table_names():
        return
    
    columns = {col['name']: col for col in inspector.get_columns('servers')}
    
    if 'credentials' in columns:
        op.drop_column('servers', 'credentials')
