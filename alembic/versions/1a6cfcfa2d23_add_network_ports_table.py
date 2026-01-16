"""add_network_ports_table

Revision ID: 1a6cfcfa2d23
Revises: 5a96fc57a0d9
Create Date: 2026-01-16 20:22:22.643247

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1a6cfcfa2d23'
down_revision: Union[str, Sequence[str], None] = '5a96fc57a0d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'network_ports',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('speed_mbps', sa.Integer(), nullable=False),
        sa.Column('lag_group', sa.String(length=255), nullable=True),
        sa.Column('monitor_bandwidth', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_network_ports_id'), 'network_ports', ['id'], unique=False)
    op.create_index(op.f('ix_network_ports_server_id'), 'network_ports', ['server_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_network_ports_server_id'), table_name='network_ports')
    op.drop_index(op.f('ix_network_ports_id'), table_name='network_ports')
    op.drop_table('network_ports')
