"""add_pxe_boot_to_network_ports

Revision ID: 6302d7c4069f
Revises: 28b2cda3387d
Create Date: 2026-01-16 20:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6302d7c4069f'
down_revision: Union[str, Sequence[str], None] = '28b2cda3387d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('network_ports', sa.Column('pxe_boot', sa.Boolean(), nullable=False, server_default='0'))
    op.add_column('network_ports', sa.Column('pxe_ip', sa.String(length=45), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('network_ports', 'pxe_ip')
    op.drop_column('network_ports', 'pxe_boot')
