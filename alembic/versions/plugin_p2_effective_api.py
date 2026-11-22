"""Plugin API 1.1: effective API version on plugin installations

Adds ``plugin_installations.effective_api`` (nullable String(16)): the highest
supported plugin API version the manifest's ``rackflow_api`` range allows,
recorded by the loader (docs/billing-phase2-design.md 8.1). Existing rows stay
NULL until the loader next sees them. On MariaDB 11 the ADD COLUMN is instant.

Revision ID: plugin_p2_effective_api
Revises: billing_p1_ledger_owner_check
Create Date: 2026-11-22 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "plugin_p2_effective_api"
down_revision: Union[str, Sequence[str], None] = "billing_p1_ledger_owner_check"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("plugin_installations", sa.Column("effective_api", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("plugin_installations", "effective_api")
