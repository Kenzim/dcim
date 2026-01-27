"""Add assets table

Revision ID: add_assets_table
Revises: add_server_uuid
Create Date: 2025-03-07

"""
from alembic import op
import sqlalchemy as sa
from typing import Union, Sequence

revision: str = "add_assets_table"
down_revision: Union[str, Sequence[str], None] = "add_server_uuid"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "assets" not in inspector.get_table_names():
        op.create_table(
            "assets",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("filename", sa.String(255), nullable=False),
            sa.Column("storage_path", sa.String(512), nullable=False),
            sa.Column("label", sa.String(64), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_assets_id", "assets", ["id"])
        op.create_index("ix_assets_storage_path", "assets", ["storage_path"], unique=True)
        op.create_index("ix_assets_label", "assets", ["label"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "assets" in inspector.get_table_names():
        op.drop_index("ix_assets_label", table_name="assets")
        op.drop_index("ix_assets_storage_path", table_name="assets")
        op.drop_index("ix_assets_id", table_name="assets")
        op.drop_table("assets")
