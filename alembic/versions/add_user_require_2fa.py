"""add require_2fa to users

Revision ID: add_user_require_2fa
Revises: add_totp_last_used_step
Create Date: 2026-10-14 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_user_require_2fa"
down_revision: Union[str, Sequence[str], None] = "add_totp_last_used_step"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table_name: str) -> set:
    return {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "require_2fa" not in _columns(inspector, "users"):
        op.add_column(
            "users",
            sa.Column("require_2fa", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "require_2fa" in _columns(inspector, "users"):
        op.drop_column("users", "require_2fa")
