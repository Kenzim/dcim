"""vm.console client permission defaults to on.

Code-level default for the vm.console client permission (VM_CONSOLE) on VM
services changes from off to on (see app.core.client_permissions). This
migration backfills the already-seeded "Power + IPMI" system preset —
described as the "default-equivalent" preset — so environments that seeded
it before this change stay consistent with the new default. Custom
(non-system) presets an admin created are left untouched.

Revision ID: vm_console_default_on
Revises: vm_tpl_strategy_opts
Create Date: 2026-07-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column
from sqlalchemy.dialects.mysql import JSON


revision: str = "vm_console_default_on"
down_revision: Union[str, None] = "vm_tpl_strategy_opts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    permission_sets = table(
        "permission_sets",
        column("id", sa.Integer),
        column("name", sa.String),
        column("is_system", sa.Boolean),
        column("permissions", JSON),
    )
    conn = op.get_bind()
    rows = conn.execute(
        sa.select(permission_sets.c.id, permission_sets.c.permissions).where(
            permission_sets.c.name == "Power + IPMI",
            permission_sets.c.is_system.is_(True),
        )
    ).fetchall()
    for row_id, permissions in rows:
        updated = dict(permissions or {})
        updated["vm.console"] = True
        conn.execute(
            permission_sets.update()
            .where(permission_sets.c.id == row_id)
            .values(permissions=updated)
        )


def downgrade() -> None:
    permission_sets = table(
        "permission_sets",
        column("id", sa.Integer),
        column("name", sa.String),
        column("is_system", sa.Boolean),
        column("permissions", JSON),
    )
    conn = op.get_bind()
    rows = conn.execute(
        sa.select(permission_sets.c.id, permission_sets.c.permissions).where(
            permission_sets.c.name == "Power + IPMI",
            permission_sets.c.is_system.is_(True),
        )
    ).fetchall()
    for row_id, permissions in rows:
        updated = dict(permissions or {})
        updated["vm.console"] = False
        conn.execute(
            permission_sets.update()
            .where(permission_sets.c.id == row_id)
            .values(permissions=updated)
        )
