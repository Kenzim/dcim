"""add_ipmi_proxy_to_servers

Revision ID: add_ipmi_proxy_to_servers
Revises: add_credentials_to_servers
Create Date: 2026-01-19 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_ipmi_proxy_to_servers'
down_revision = 'add_scripts_table'  # Latest migration in the chain
branch_labels = None
depends_on = None


def upgrade():
    # Add IPMI proxy configuration columns to servers table
    op.add_column('servers', sa.Column('ipmi_proxy_enabled', sa.Boolean(), nullable=False, server_default='0'))
    op.add_column('servers', sa.Column('ipmi_web_management_url', sa.String(length=512), nullable=True))
    op.add_column('servers', sa.Column('ipmi_viewer_username', sa.String(length=255), nullable=True))
    op.add_column('servers', sa.Column('ipmi_viewer_password', sa.String(length=255), nullable=True))


def downgrade():
    # Remove IPMI proxy configuration columns
    op.drop_column('servers', 'ipmi_viewer_password')
    op.drop_column('servers', 'ipmi_viewer_username')
    op.drop_column('servers', 'ipmi_web_management_url')
    op.drop_column('servers', 'ipmi_proxy_enabled')
