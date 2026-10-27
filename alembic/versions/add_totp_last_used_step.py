"""add last_used_step to user_totp_secrets for TOTP replay protection

Revision ID: add_totp_last_used_step
Revises: widen_installation_task_logs
Create Date: 2026-10-14 15:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_totp_last_used_step"
down_revision: Union[str, Sequence[str], None] = "widen_installation_task_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table_name: str) -> set:
    return {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "last_used_step" not in _columns(inspector, "user_totp_secrets"):
        op.add_column(
            "user_totp_secrets",
            sa.Column("last_used_step", sa.BigInteger(), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "last_used_step" in _columns(inspector, "user_totp_secrets"):
        op.drop_column("user_totp_secrets", "last_used_step")
