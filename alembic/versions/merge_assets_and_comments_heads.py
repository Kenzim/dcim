"""Merge heads: add_assets_table and add_comments_to_servers

Revision ID: merge_assets_comments
Revises: add_assets_table, add_comments_to_servers
Create Date: 2026-03-07

"""
from typing import Sequence, Union
from alembic import op


revision: str = "merge_assets_comments"
down_revision: Union[str, Sequence[str], None] = ("add_assets_table", "add_comments_to_servers")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
