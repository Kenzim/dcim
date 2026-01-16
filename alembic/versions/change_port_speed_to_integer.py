"""change port_speed to integer in mbps

Revision ID: change_port_speed_int
Revises: add_locations_disks
Create Date: 2026-01-14 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'change_port_speed_int'
down_revision: Union[str, Sequence[str], None] = 'add_locations_disks'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Check if column exists and what type it is
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'servers' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('servers')}
        
        # If port_speed exists as String, rename it to port_speed_mbps and change type
        if 'port_speed' in columns and 'port_speed_mbps' not in columns:
            # First, add new integer column
            op.add_column('servers', sa.Column('port_speed_mbps', sa.Integer(), nullable=True))
            
            # Try to convert existing string values to integers (e.g., "1Gbps" -> 1000)
            # This is a best-effort conversion
            op.execute("""
                UPDATE servers 
                SET port_speed_mbps = CASE
                    WHEN port_speed LIKE '%Gbps' THEN CAST(REPLACE(port_speed, 'Gbps', '') AS UNSIGNED) * 1000
                    WHEN port_speed LIKE '%Mbps' THEN CAST(REPLACE(port_speed, 'Mbps', '') AS UNSIGNED)
                    WHEN port_speed REGEXP '^[0-9]+$' THEN CAST(port_speed AS UNSIGNED)
                    ELSE NULL
                END
                WHERE port_speed IS NOT NULL AND port_speed != ''
            """)
            
            # Drop old column
            op.drop_column('servers', 'port_speed')


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'servers' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('servers')}
        
        # If port_speed_mbps exists, convert back to string
        if 'port_speed_mbps' in columns and 'port_speed' not in columns:
            # Add string column
            op.add_column('servers', sa.Column('port_speed', sa.String(length=50), nullable=True))
            
            # Convert integer back to string format (e.g., 1000 -> "1Gbps")
            op.execute("""
                UPDATE servers 
                SET port_speed = CASE
                    WHEN port_speed_mbps >= 1000 AND port_speed_mbps % 1000 = 0 THEN CONCAT(port_speed_mbps / 1000, 'Gbps')
                    ELSE CONCAT(port_speed_mbps, 'Mbps')
                END
                WHERE port_speed_mbps IS NOT NULL
            """)
            
            # Drop integer column
            op.drop_column('servers', 'port_speed_mbps')



