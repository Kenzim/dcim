"""IP reselling: max_resale_count on subnets, drop unique on assignment.ip_id

Allows a single proxy IP to be resold to more than one service at once:

1. ``ip_subnets.max_resale_count`` (default 1, matching today's exclusive
   behavior) caps how many concurrent ``ServiceIPAssignment`` rows an IP in
   that subnet may carry.
2. The old ``uq_service_ip_assignments_ip_id`` unique constraint is dropped
   so more than one assignment can reference the same ``ip_id`` — allocation
   and the same-owner collision guard now live in application code
   (``IPAMDAO``), not the schema.

Purely additive/relaxing: existing subnets get ``max_resale_count = 1``,
which reproduces current exclusive-IP behavior exactly, and no existing rows
change shape.

Revision ID: ipam_resale_support
Revises: identity_collapse_users
Create Date: 2026-07-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ipam_resale_support"
down_revision: Union[str, None] = "identity_collapse_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_column(inspector, table: str, column: str) -> bool:
    if not _has_table(inspector, table):
        return False
    return column in [c["name"] for c in inspector.get_columns(table)]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if _has_table(inspector, "ip_subnets") and not _has_column(inspector, "ip_subnets", "max_resale_count"):
        op.add_column(
            "ip_subnets",
            sa.Column("max_resale_count", sa.Integer(), nullable=False, server_default="1"),
        )
        # Drop the server_default once existing rows are backfilled so the
        # ORM (which always supplies a value) is the sole source of truth
        # going forward, same convention as other non-nullable columns here.
        op.alter_column("ip_subnets", "max_resale_count", server_default=None)

    if _has_table(inspector, "service_ip_assignments"):
        uniques = [uc["name"] for uc in inspector.get_unique_constraints("service_ip_assignments")]
        if "uq_service_ip_assignments_ip_id" in uniques:
            op.drop_constraint("uq_service_ip_assignments_ip_id", "service_ip_assignments", type_="unique")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if _has_table(inspector, "service_ip_assignments"):
        uniques = [uc["name"] for uc in inspector.get_unique_constraints("service_ip_assignments")]
        if "uq_service_ip_assignments_ip_id" not in uniques:
            # If any IP was actually resold under max_resale_count > 1, this
            # will fail with a duplicate-key error — that's intentional:
            # downgrading past this revision is only safe once no IP holds
            # more than one live assignment.
            op.create_unique_constraint(
                "uq_service_ip_assignments_ip_id", "service_ip_assignments", ["ip_id"]
            )

    if _has_table(inspector, "ip_subnets") and _has_column(inspector, "ip_subnets", "max_resale_count"):
        op.drop_column("ip_subnets", "max_resale_count")
