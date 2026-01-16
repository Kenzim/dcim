"""add CASCADE to plugin_categories foreign keys

Revision ID: add_cascade_plugin_categories
Revises: make_location_required
Create Date: 2026-01-14 23:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_cascade_plugin_categories'
down_revision: Union[str, Sequence[str], None] = 'make_location_required'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    # Check if plugin_categories table exists
    if 'plugin_categories' in inspector.get_table_names():
        # Get existing foreign keys
        fks = inspector.get_foreign_keys('plugin_categories')
        
        # Drop existing foreign keys
        for fk in fks:
            op.drop_constraint(fk['name'], 'plugin_categories', type_='foreignkey')
        
        # Recreate foreign keys with CASCADE
        op.create_foreign_key(
            'plugin_categories_ibfk_1',
            'plugin_categories', 'plugins',
            ['plugin_id'], ['id'],
            ondelete='CASCADE'
        )
        op.create_foreign_key(
            'plugin_categories_ibfk_2',
            'plugin_categories', 'categories',
            ['category_id'], ['id'],
            ondelete='CASCADE'
        )


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if 'plugin_categories' in inspector.get_table_names():
        # Drop CASCADE foreign keys
        fks = inspector.get_foreign_keys('plugin_categories')
        for fk in fks:
            op.drop_constraint(fk['name'], 'plugin_categories', type_='foreignkey')
        
        # Recreate without CASCADE
        op.create_foreign_key(
            'plugin_categories_ibfk_1',
            'plugin_categories', 'plugins',
            ['plugin_id'], ['id']
        )
        op.create_foreign_key(
            'plugin_categories_ibfk_2',
            'plugin_categories', 'categories',
            ['category_id'], ['id']
        )



