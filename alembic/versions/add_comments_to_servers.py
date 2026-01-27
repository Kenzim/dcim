"""add_comments_to_servers

Revision ID: add_comments_to_servers
Revises: add_ipmi_proxy_to_servers
Create Date: 2026-03-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'add_comments_to_servers'
down_revision = 'add_ipmi_proxy_to_servers'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('servers', sa.Column('comments', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('servers', 'comments')
