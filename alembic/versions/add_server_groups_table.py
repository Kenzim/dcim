"""Add server_groups table and server_group_association junction table

Revision ID: add_server_groups
Revises: add_switch_ports_and_cable_runs
Create Date: 2025-01-26 21:45:00.000000

"""
from alembic import op
import sqlalchemy as sa
from typing import Union, Sequence

# revision identifiers, used by Alembic.
revision: str = 'add_server_groups'
down_revision: Union[str, Sequence[str], None] = 'remove_mgmt_ip'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    # Create server_groups table
    if 'server_groups' not in inspector.get_table_names():
        op.create_table(
            'server_groups',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_server_groups_id', 'server_groups', ['id'])
        op.create_index('ix_server_groups_name', 'server_groups', ['name'], unique=True)
    
    # Create server_group_association junction table
    if 'server_group_association' not in inspector.get_table_names():
        op.create_table(
            'server_group_association',
            sa.Column('server_id', sa.Integer(), nullable=False),
            sa.Column('server_group_id', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['server_group_id'], ['server_groups.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('server_id', 'server_group_id')
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    # Drop junction table first
    if 'server_group_association' in inspector.get_table_names():
        op.drop_table('server_group_association')
    
    # Drop server_groups table
    if 'server_groups' in inspector.get_table_names():
        op.drop_table('server_groups')
