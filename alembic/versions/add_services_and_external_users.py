"""Add services and external_users tables

Revision ID: add_services_external_users
Revises: add_billing_integrations
Create Date: 2025-01-18 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from typing import Union, Sequence

# revision identifiers, used by Alembic.
revision: str = 'add_services_external_users'
down_revision: Union[str, Sequence[str], None] = 'add_billing_integrations'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    # Create external_users table
    if 'external_users' not in inspector.get_table_names():
        op.create_table(
            'external_users',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('integration_id', sa.Integer(), nullable=False),
            sa.Column('external_user_id', sa.String(255), nullable=False),
            sa.Column('external_username', sa.String(255), nullable=True),
            sa.Column('external_email', sa.String(255), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.ForeignKeyConstraint(['integration_id'], ['billing_integrations.id'], ),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_external_users_id', 'external_users', ['id'])
        op.create_index('ix_external_users_integration_id', 'external_users', ['integration_id'])
        op.create_index('ix_external_user_integration_external_id', 'external_users', ['integration_id', 'external_user_id'], unique=True)
    
    # Add external_user_id to servers table
    if 'servers' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('servers')]
        if 'external_user_id' not in columns:
            op.add_column('servers', sa.Column('external_user_id', sa.Integer(), nullable=True))
            op.create_foreign_key('fk_servers_external_user_id', 'servers', 'external_users', ['external_user_id'], ['id'])
            op.create_index('ix_servers_external_user_id', 'servers', ['external_user_id'])
    
    # Create services table
    if 'services' not in inspector.get_table_names():
        op.create_table(
            'services',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('external_service_id', sa.String(255), nullable=True),
            sa.Column('server_id', sa.Integer(), nullable=False),
            sa.Column('external_user_id', sa.Integer(), nullable=False),
            sa.Column('status', sa.String(50), nullable=False, server_default='pending'),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('config', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('terminated_at', sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ),
            sa.ForeignKeyConstraint(['external_user_id'], ['external_users.id'], ),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_services_id', 'services', ['id'])
        op.create_index('ix_services_name', 'services', ['name'])
        op.create_index('ix_services_external_service_id', 'services', ['external_service_id'])
        op.create_index('ix_services_server_id', 'services', ['server_id'])
        op.create_index('ix_services_external_user_id', 'services', ['external_user_id'])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    # Drop services table
    if 'services' in inspector.get_table_names():
        op.drop_index('ix_services_external_user_id', table_name='services')
        op.drop_index('ix_services_server_id', table_name='services')
        op.drop_index('ix_services_external_service_id', table_name='services')
        op.drop_index('ix_services_name', table_name='services')
        op.drop_index('ix_services_id', table_name='services')
        op.drop_table('services')
    
    # Remove external_user_id from servers table
    if 'servers' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('servers')]
        if 'external_user_id' in columns:
            op.drop_index('ix_servers_external_user_id', table_name='servers')
            op.drop_constraint('fk_servers_external_user_id', 'servers', type_='foreignkey')
            op.drop_column('servers', 'external_user_id')
    
    # Drop external_users table
    if 'external_users' in inspector.get_table_names():
        op.drop_index('ix_external_user_integration_external_id', table_name='external_users')
        op.drop_index('ix_external_users_integration_id', table_name='external_users')
        op.drop_index('ix_external_users_id', table_name='external_users')
        op.drop_table('external_users')
