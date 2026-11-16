"""add user_invite_tokens

Revision ID: add_user_invite_tokens
Revises: add_server_lock
Create Date: 2026-11-16 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_user_invite_tokens"
down_revision: Union[str, Sequence[str], None] = "add_server_lock"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "user_invite_tokens" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "user_invite_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("invited_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("username_prefilled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_user_invite_tokens_id", "user_invite_tokens", ["id"])
    op.create_index("ix_user_invite_tokens_user_id", "user_invite_tokens", ["user_id"])
    op.create_index("ix_user_invite_tokens_token_hash", "user_invite_tokens", ["token_hash"], unique=True)
    op.create_index("ix_user_invite_tokens_expires_at", "user_invite_tokens", ["expires_at"])


def downgrade() -> None:
    if "user_invite_tokens" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("user_invite_tokens")
