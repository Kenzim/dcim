"""Add billing_integrations table

Revision ID: add_billing_integrations
Revises: add_pxe_os_boot_mode
Create Date: 2025-01-18 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from typing import Union, Sequence

# revision identifiers, used by Alembic.
revision: str = 'add_billing_integrations'
down_revision: Union[str, Sequence[str], None] = 'add_pxe_os_boot_mode'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'billing_integrations' not in inspector.get_table_names():
        op.create_table(
            'billing_integrations',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('integration_type', sa.String(50), nullable=False),
            sa.Column('api_key', sa.String(255), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1'),
            sa.Column('config', sa.JSON(), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_used_ip', sa.String(45), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_billing_integrations_id', 'billing_integrations', ['id'])
        op.create_index('ix_billing_integrations_name', 'billing_integrations', ['name'])
        op.create_index('ix_billing_integrations_integration_type', 'billing_integrations', ['integration_type'])
        op.create_index('ix_billing_integrations_api_key', 'billing_integrations', ['api_key'], unique=True)
        op.create_index('ix_billing_integrations_enabled', 'billing_integrations', ['enabled'])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'billing_integrations' in inspector.get_table_names():
        op.drop_index('ix_billing_integrations_enabled', table_name='billing_integrations')
        op.drop_index('ix_billing_integrations_api_key', table_name='billing_integrations')
        op.drop_index('ix_billing_integrations_integration_type', table_name='billing_integrations')
        op.drop_index('ix_billing_integrations_name', table_name='billing_integrations')
        op.drop_index('ix_billing_integrations_id', table_name='billing_integrations')
        op.drop_table('billing_integrations')
