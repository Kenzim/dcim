"""add_mac_address_to_network_ports

Revision ID: 28b2cda3387d
Revises: 1a6cfcfa2d23
Create Date: 2026-01-16 20:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '28b2cda3387d'
down_revision: Union[str, Sequence[str], None] = '1a6cfcfa2d23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('network_ports', sa.Column('mac_address', sa.String(length=17), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('network_ports', 'mac_address')
