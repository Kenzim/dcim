"""Add locations.proxy_port

Revision ID: add_location_proxy_port
Revises: add_client_mcp_api_keys
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "add_location_proxy_port"
down_revision: Union[str, Sequence[str], None] = "add_client_mcp_api_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("locations", sa.Column("proxy_port", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("locations", "proxy_port")
