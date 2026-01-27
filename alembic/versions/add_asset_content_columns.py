"""Add content and content_type to assets (store image bytes in DB)

Revision ID: add_asset_content
Revises: add_rack_units_switches
Create Date: 2025-03-07

"""
from alembic import op
import sqlalchemy as sa
from typing import Union, Sequence

revision: str = "add_asset_content"
down_revision: Union[str, Sequence[str], None] = "add_rack_units_switches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "assets" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("assets")]
    if "content" not in columns:
        op.add_column("assets", sa.Column("content", sa.LargeBinary(), nullable=True))
    if "content_type" not in columns:
        op.add_column("assets", sa.Column("content_type", sa.String(128), nullable=True))
    # Make storage_path nullable so new assets can have content-only (no file on disk)
    try:
        op.alter_column(
            "assets",
            "storage_path",
            existing_type=sa.String(512),
            nullable=True,
        )
    except Exception:
        pass  # Already nullable or DB-specific


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "assets" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("assets")]
    if "content" in columns:
        op.drop_column("assets", "content")
    if "content_type" in columns:
        op.drop_column("assets", "content_type")
    try:
        op.alter_column(
            "assets",
            "storage_path",
            existing_type=sa.String(512),
            nullable=False,
        )
    except Exception:
        pass
