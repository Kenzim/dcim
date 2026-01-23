"""add_os_disk_flag_and_serial_to_disks

Revision ID: add_os_disk_serial
Revises: e4a3f01f2b09, add_boot_tasks
Create Date: 2026-01-18 02:15:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_os_disk_serial'
down_revision: Union[str, Sequence[str], None] = ('e4a3f01f2b09', 'add_boot_tasks')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add is_os_disk flag and serial_number to disks table"""
    # Check if disks table exists
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'disks' not in inspector.get_table_names():
        return  # Table doesn't exist yet
    
    columns = {col['name']: col for col in inspector.get_columns('disks')}
    
    # Add serial_number column if it doesn't exist
    if 'serial_number' not in columns:
        op.add_column('disks', sa.Column('serial_number', sa.String(255), nullable=True))
        op.create_index('ix_disks_serial_number', 'disks', ['serial_number'])
    
    # Add is_os_disk column if it doesn't exist
    if 'is_os_disk' not in columns:
        op.add_column('disks', sa.Column('is_os_disk', sa.Boolean(), nullable=False, server_default='0'))
        op.create_index('ix_disks_is_os_disk', 'disks', ['is_os_disk'])


def downgrade() -> None:
    """Remove is_os_disk flag and serial_number from disks table"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'disks' not in inspector.get_table_names():
        return
    
    columns = {col['name']: col for col in inspector.get_columns('disks')}
    
    # Remove indexes first
    if 'is_os_disk' in columns:
        try:
            op.drop_index('ix_disks_is_os_disk', table_name='disks')
        except Exception:
            pass
        op.drop_column('disks', 'is_os_disk')
    
    if 'serial_number' in columns:
        try:
            op.drop_index('ix_disks_serial_number', table_name='disks')
        except Exception:
            pass
        op.drop_column('disks', 'serial_number')
