"""Add preview_asset_id to servers

Revision ID: add_preview_asset_servers
Revises: merge_assets_comments
Create Date: 2026-03-07

"""
from alembic import op
import sqlalchemy as sa


revision = 'add_preview_asset_servers'
down_revision = 'merge_assets_comments'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('servers', sa.Column('preview_asset_id', sa.Integer(), nullable=True, index=True))
    op.create_foreign_key('fk_servers_preview_asset_id', 'servers', 'assets', ['preview_asset_id'], ['id'])


def downgrade():
    op.drop_constraint('fk_servers_preview_asset_id', 'servers', type_='foreignkey')
    op.drop_column('servers', 'preview_asset_id')
