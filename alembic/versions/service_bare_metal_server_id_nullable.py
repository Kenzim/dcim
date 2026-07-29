"""Make service_bare_metal.server_id nullable for server-less http_proxy services.

HTTP-proxy products provisioned purely from the IP pool (IPAM) don't need a
rack Server row. Bare-metal services still always have a server; this only
relaxes the column so http_proxy creation can skip the placeholder Server.

Revision ID: service_bare_metal_server_id_nullable
Revises: vm_template_code
Create Date: 2026-07-27
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "service_bare_metal_server_id_nullable"
down_revision: Union[str, None] = "vm_template_code"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("service_bare_metal") as batch_op:
        batch_op.alter_column(
            "server_id",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade() -> None:
    # Any server-less rows would violate NOT NULL; leave them for an operator
    # to link a server or delete before downgrading.
    with op.batch_alter_table("service_bare_metal") as batch_op:
        batch_op.alter_column(
            "server_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
