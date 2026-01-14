"""add plugins and servers tables

Revision ID: add_plugin_servers
Revises: 37f7f16908f5
Create Date: 2026-01-14 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


# revision identifiers, used by Alembic.
revision: str = 'add_plugin_servers'
down_revision: Union[str, Sequence[str], None] = '37f7f16908f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create plugins table
    op.create_table('plugins',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('version', sa.String(length=50), nullable=False),
    sa.Column('supported_categories', sa.JSON(), nullable=True),  # Will be removed in next migration
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('config_template', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_plugins_id'), 'plugins', ['id'], unique=False)
    op.create_index(op.f('ix_plugins_name'), 'plugins', ['name'], unique=True)
    
    # Create servers table
    op.create_table('servers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('hostname', sa.String(length=255), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('plugin_id', sa.Integer(), nullable=False),
    sa.Column('plugin_config', sa.JSON(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
    sa.ForeignKeyConstraint(['plugin_id'], ['plugins.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_servers_id'), 'servers', ['id'], unique=False)
    op.create_index(op.f('ix_servers_hostname'), 'servers', ['hostname'], unique=True)
    op.create_index(op.f('ix_servers_plugin_id'), 'servers', ['plugin_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_servers_plugin_id'), table_name='servers')
    op.drop_index(op.f('ix_servers_hostname'), table_name='servers')
    op.drop_index(op.f('ix_servers_id'), table_name='servers')
    op.drop_table('servers')
    op.drop_index(op.f('ix_plugins_name'), table_name='plugins')
    op.drop_index(op.f('ix_plugins_id'), table_name='plugins')
    op.drop_table('plugins')
