"""remove_management_ip_from_switches

Revision ID: remove_management_ip_from_switches
Revises: merge_heads_001
Create Date: 2026-01-23 14:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'remove_mgmt_ip'
down_revision: Union[str, Sequence[str], None] = 'merge_heads_001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove management_ip column from network_switches table"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    if 'network_switches' in tables:
        columns = [col['name'] for col in inspector.get_columns('network_switches')]
        if 'management_ip' in columns:
            # Drop the management_ip column
            op.drop_column('network_switches', 'management_ip')


def downgrade() -> None:
    """Add management_ip column back to network_switches table"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    if 'network_switches' in tables:
        columns = [col['name'] for col in inspector.get_columns('network_switches')]
        if 'management_ip' not in columns:
            # Add the management_ip column back
            op.add_column('network_switches', sa.Column('management_ip', sa.String(length=45), nullable=False))
