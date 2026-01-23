"""Add scripts table

Revision ID: add_scripts_table
Revises: add_services_external_users
Create Date: 2025-01-18 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from typing import Union, Sequence

# revision identifiers, used by Alembic.
revision: str = 'add_scripts_table'
down_revision: Union[str, Sequence[str], None] = 'add_services_external_users'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'scripts' not in inspector.get_table_names():
        op.create_table(
            'scripts',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1'),
            sa.Column('user_executable', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_scripts_id', 'scripts', ['id'])
        op.create_index('ix_scripts_name', 'scripts', ['name'], unique=True)
        op.create_index('ix_scripts_enabled', 'scripts', ['enabled'])
        op.create_index('ix_scripts_user_executable', 'scripts', ['user_executable'])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'scripts' in inspector.get_table_names():
        op.drop_index('ix_scripts_user_executable', table_name='scripts')
        op.drop_index('ix_scripts_enabled', table_name='scripts')
        op.drop_index('ix_scripts_name', table_name='scripts')
        op.drop_index('ix_scripts_id', table_name='scripts')
        op.drop_table('scripts')
