"""add units_start_from_bottom to racks

Revision ID: racks_units_orientation
Revises: add_asset_content
Create Date: 2026-03-16 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'racks_units_orientation'
down_revision: Union[str, Sequence[str], None] = 'add_asset_content'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
  """Add units_start_from_bottom boolean column to racks if missing."""
  conn = op.get_bind()
  inspector = sa.inspect(conn)

  if 'racks' in inspector.get_table_names():
    columns = {col['name']: col for col in inspector.get_columns('racks')}
    if 'units_start_from_bottom' not in columns:
      op.add_column(
        'racks',
        sa.Column('units_start_from_bottom', sa.Boolean(), nullable=False, server_default=sa.text('1')),
      )


def downgrade() -> None:
  """Remove units_start_from_bottom column from racks."""
  conn = op.get_bind()
  inspector = sa.inspect(conn)

  if 'racks' in inspector.get_table_names():
    columns = {col['name']: col for col in inspector.get_columns('racks')}
    if 'units_start_from_bottom' in columns:
      op.drop_column('racks', 'units_start_from_bottom')

