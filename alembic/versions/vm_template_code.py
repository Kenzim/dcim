"""Add immutable unique VMTemplate.code.

Revision ID: vm_template_code
Revises: vm_console_default_on
Create Date: 2026-07-27
"""
from __future__ import annotations

import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "vm_template_code"
down_revision: Union[str, None] = "vm_console_default_on"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _slugify(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")[:128]
    return text or "template"


def upgrade() -> None:
    op.add_column("vm_templates", sa.Column("code", sa.String(length=128), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, name, proxmox_template_name FROM vm_templates ORDER BY id")
    ).fetchall()
    used: set[str] = set()
    for row in rows:
        tmpl_id, name, proxmox_name = row[0], row[1], row[2]
        base = _slugify(proxmox_name or name or f"template-{tmpl_id}")
        code = base
        n = 2
        while code in used:
            suffix = f"-{n}"
            code = f"{base[: max(1, 128 - len(suffix))]}{suffix}"
            n += 1
        used.add(code)
        conn.execute(
            sa.text("UPDATE vm_templates SET code = :code WHERE id = :id"),
            {"code": code, "id": tmpl_id},
        )

    op.alter_column("vm_templates", "code", existing_type=sa.String(length=128), nullable=False)
    op.create_index("ix_vm_templates_code", "vm_templates", ["code"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_vm_templates_code", table_name="vm_templates")
    op.drop_column("vm_templates", "code")
