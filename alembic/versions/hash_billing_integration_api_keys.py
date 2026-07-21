"""hash billing integration api keys

Adds api_key_prefix and replaces plaintext api_key values with their SHA-256
hash so that keys are no longer stored in cleartext. Existing WHMCS/custom
integrations keep working because authentication hashes the presented key and
compares it to the stored hash.

Revision ID: hash_billing_api_keys
Revises: add_server_pxe_kernel_args
Create Date: 2026-07-21 00:00:00.000000
"""
import hashlib
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


# revision identifiers, used by Alembic.
revision: str = "hash_billing_api_keys"
down_revision: Union[str, Sequence[str], None] = "add_server_pxe_kernel_args"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _hash(value: str) -> str:
    return hashlib.sha256((value or "").strip().encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.add_column(
        "billing_integrations",
        sa.Column("api_key_prefix", sa.String(length=16), nullable=True),
    )

    # Hash any existing plaintext keys in place and record a display prefix.
    bi = table(
        "billing_integrations",
        column("id", sa.Integer),
        column("api_key", sa.String),
        column("api_key_prefix", sa.String),
    )
    conn = op.get_bind()
    rows = conn.execute(sa.select(bi.c.id, bi.c.api_key)).fetchall()
    for row_id, api_key in rows:
        if not api_key or _SHA256_HEX.match(api_key):
            # Already hashed (or empty) - leave as-is.
            continue
        conn.execute(
            bi.update()
            .where(bi.c.id == row_id)
            .values(api_key=_hash(api_key), api_key_prefix=api_key[:8])
        )


def downgrade() -> None:
    # Plaintext cannot be recovered from a hash; only drop the added column.
    op.drop_column("billing_integrations", "api_key_prefix")
