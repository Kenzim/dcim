"""Add pxe_boot_mode and os_boot_mode to servers

Revision ID: add_pxe_os_boot_mode
Revises: add_server_credentials
Create Date: 2025-01-18 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from typing import Union, Sequence

# revision identifiers, used by Alembic.
revision: str = 'add_pxe_os_boot_mode'
down_revision: Union[str, Sequence[str], None] = 'add_server_credentials'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'servers' not in inspector.get_table_names():
        return
    
    columns = {col['name']: col for col in inspector.get_columns('servers')}
    
    # Add pxe_boot_mode column
    if 'pxe_boot_mode' not in columns:
        op.add_column('servers', sa.Column('pxe_boot_mode', sa.String(10), nullable=False, server_default='uefi'))
        op.create_index('ix_servers_pxe_boot_mode', 'servers', ['pxe_boot_mode'])
    
    # Add os_boot_mode column
    if 'os_boot_mode' not in columns:
        op.add_column('servers', sa.Column('os_boot_mode', sa.String(10), nullable=False, server_default='uefi'))
        op.create_index('ix_servers_os_boot_mode', 'servers', ['os_boot_mode'])
    
    # Migrate existing boot_mode values to both new fields
    # This ensures backward compatibility
    op.execute("""
        UPDATE servers 
        SET pxe_boot_mode = boot_mode, 
            os_boot_mode = boot_mode
    """)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'servers' not in inspector.get_table_names():
        return
    
    columns = {col['name']: col for col in inspector.get_columns('servers')}
    
    if 'os_boot_mode' in columns:
        op.drop_index('ix_servers_os_boot_mode', table_name='servers')
        op.drop_column('servers', 'os_boot_mode')
    
    if 'pxe_boot_mode' in columns:
        op.drop_index('ix_servers_pxe_boot_mode', table_name='servers')
        op.drop_column('servers', 'pxe_boot_mode')
