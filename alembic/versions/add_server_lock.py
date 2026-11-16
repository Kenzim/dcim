"""add servers.locked and servers.lock_reason

Revision ID: add_server_lock
Revises: add_user_require_2fa
Create Date: 2026-11-16 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_server_lock"
down_revision: Union[str, Sequence[str], None] = "add_user_require_2fa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("servers")}


def upgrade() -> None:
    cols = _columns()
    if "locked" not in cols:
        op.add_column(
            "servers",
            sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "lock_reason" not in cols:
        op.add_column("servers", sa.Column("lock_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    cols = _columns()
    if "lock_reason" in cols:
        op.drop_column("servers", "lock_reason")
    if "locked" in cols:
        op.drop_column("servers", "locked")
