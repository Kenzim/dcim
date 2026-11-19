"""Drop private_network_pools.proxy_access_requires_product.

The proxy fast path is not sold: clients with an active server and proxy in a
location may turn it on, so the pool switch that required a product is gone.

Revision ID: drop_proxy_access_requires_product
Revises: add_storage_sftp
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "drop_proxy_access_requires_product"
down_revision: Union[str, Sequence[str], None] = "add_storage_sftp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return "proxy_access_requires_product" in {c["name"] for c in inspector.get_columns("private_network_pools")}


def upgrade() -> None:
    if _has_column():
        op.drop_column("private_network_pools", "proxy_access_requires_product")


def downgrade() -> None:
    if not _has_column():
        op.add_column(
            "private_network_pools",
            sa.Column("proxy_access_requires_product", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
