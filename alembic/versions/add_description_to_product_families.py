"""add description to product families

Revision ID: add_pf_desc
Revises: make_product_family_optional
Create Date: 2026-03-20 13:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_pf_desc"
down_revision: Union[str, Sequence[str], None] = "make_product_family_optional"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "product_families" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("product_families")]
    if "description" not in columns:
        op.add_column("product_families", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "product_families" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("product_families")]
    if "description" in columns:
        op.drop_column("product_families", "description")
