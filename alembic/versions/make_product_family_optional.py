"""make product family optional

Revision ID: make_product_family_optional
Revises: add_vm_proxy_platform_foundation
Create Date: 2026-03-20 13:20:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "make_product_family_optional"
down_revision: Union[str, Sequence[str], None] = "add_vm_proxy_platform_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "products" not in inspector.get_table_names():
        return
    columns = {c["name"]: c for c in inspector.get_columns("products")}
    if "family_id" in columns and not columns["family_id"].get("nullable", False):
        op.alter_column("products", "family_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "products" not in inspector.get_table_names():
        return
    op.alter_column("products", "family_id", existing_type=sa.Integer(), nullable=False)
