"""make location_id required for servers

Revision ID: make_location_required
Revises: change_port_speed_int
Create Date: 2026-01-14 23:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'make_location_required'
down_revision: Union[str, Sequence[str], None] = 'change_port_speed_int'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'servers' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('servers')}
        
        # If location_id exists and is nullable, make it NOT NULL
        if 'location_id' in columns and columns['location_id']['nullable']:
            # First, set a default location for any servers without one
            # Get first location or create a default one
            result = conn.execute(sa.text("SELECT id FROM locations LIMIT 1"))
            first_location = result.fetchone()
            
            if first_location:
                default_location_id = first_location[0]
                # Update servers without location
                op.execute(f"UPDATE servers SET location_id = {default_location_id} WHERE location_id IS NULL")
            else:
                # Create a default location if none exists
                op.execute("""
                    INSERT INTO locations (name, description) 
                    VALUES ('Default Location', 'Default location for existing servers')
                """)
                result = conn.execute(sa.text("SELECT id FROM locations WHERE name = 'Default Location'"))
                default_location_id = result.fetchone()[0]
                op.execute(f"UPDATE servers SET location_id = {default_location_id} WHERE location_id IS NULL")
            
            # Now make it NOT NULL
            op.alter_column('servers', 'location_id', nullable=False, existing_type=sa.Integer())


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'servers' in inspector.get_table_names():
        columns = {col['name']: col for col in inspector.get_columns('servers')}
        
        # Make location_id nullable again
        if 'location_id' in columns and not columns['location_id']['nullable']:
            op.alter_column('servers', 'location_id', nullable=True, existing_type=sa.Integer())



