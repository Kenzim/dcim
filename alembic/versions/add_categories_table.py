"""add categories table and relationship

Revision ID: add_categories
Revises: add_plugin_servers
Create Date: 2026-01-14 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_categories'
down_revision: Union[str, Sequence[str], None] = 'add_plugin_servers'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create categories table
    op.create_table('categories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_categories_id'), 'categories', ['id'], unique=False)
    op.create_index(op.f('ix_categories_name'), 'categories', ['name'], unique=True)
    
    # Create plugin_categories junction table
    op.create_table('plugin_categories',
    sa.Column('plugin_id', sa.Integer(), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['plugin_id'], ['plugins.id'], ),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ),
    sa.PrimaryKeyConstraint('plugin_id', 'category_id')
    )
    
    # Remove supported_categories JSON column from plugins (data migration would go here)
    # For now, we'll drop it - existing data would need to be migrated first
    op.drop_column('plugins', 'supported_categories')
    
    # Seed default categories (using INSERT IGNORE for MySQL compatibility)
    op.execute("""
        INSERT IGNORE INTO categories (name, display_name, description) VALUES
        ('power_control', 'Power Control', 'Control server power state (on, off, reset)'),
        ('user_account_control', 'User Account Control', 'Manage user accounts on the server'),
        ('boot_order_control', 'Boot Order Control', 'Manage boot order and boot device selection')
    """)


def downgrade() -> None:
    """Downgrade schema."""
    # Add back supported_categories column
    op.add_column('plugins', sa.Column('supported_categories', sa.JSON(), nullable=True))
    
    # Drop junction table
    op.drop_table('plugin_categories')
    
    # Drop categories table
    op.drop_index(op.f('ix_categories_name'), table_name='categories')
    op.drop_index(op.f('ix_categories_id'), table_name='categories')
    op.drop_table('categories')

