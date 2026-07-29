"""users.password nullable (blank password disables password login)

Non-admin (client) accounts always have full client portal access via
admin impersonation and billing SSO; a blank/unset password only disables
the direct username/password login path until an admin sets a real one.
Newly auto-provisioned billing-linked accounts are created with no password.

Revision ID: users_password_nullable
Revises: hash_billing_api_keys
Create Date: 2026-07-23 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "users_password_nullable"
down_revision: Union[str, Sequence[str], None] = "hash_billing_api_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "password",
        existing_type=sa.String(255),
        nullable=True,
    )


def downgrade() -> None:
    # Any accounts with a blank password would violate NOT NULL again; give
    # them an unusable-but-non-null placeholder so the downgrade doesn't fail.
    op.execute("UPDATE users SET password = '' WHERE password IS NULL")
    op.alter_column(
        "users",
        "password",
        existing_type=sa.String(255),
        nullable=False,
    )
