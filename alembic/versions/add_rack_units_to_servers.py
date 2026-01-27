"""Add rack_units to servers (number of U a server occupies)

Revision ID: add_rack_units_servers
Revises: add_preview_asset_servers
Create Date: 2026-03-07

"""
from alembic import op
import sqlalchemy as sa


revision = 'add_rack_units_servers'
down_revision = 'add_preview_asset_servers'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('servers')]
    if 'rack_units' not in columns:
        op.add_column('servers', sa.Column('rack_units', sa.Integer(), nullable=True))
        op.execute(sa.text("UPDATE servers SET rack_units = 1 WHERE rack_units IS NULL"))
        op.alter_column('servers', 'rack_units', nullable=False, server_default=sa.text('1'))


def downgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('servers')]
    if 'rack_units' in columns:
        op.drop_column('servers', 'rack_units')
