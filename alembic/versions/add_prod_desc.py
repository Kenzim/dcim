"""add description column to products

Revision ID: add_prod_desc
Revises: add_vm_cfg
Create Date: 2026-03-20 15:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_prod_desc"
down_revision: Union[str, Sequence[str], None] = "add_vm_cfg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "products" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("products")]
    if "description" not in columns:
        op.add_column("products", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "products" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("products")]
    if "description" in columns:
        op.drop_column("products", "description")
