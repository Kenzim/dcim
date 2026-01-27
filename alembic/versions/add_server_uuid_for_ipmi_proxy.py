"""Add uuid to servers for IPMI proxy

Revision ID: add_server_uuid
Revises: add_si_id_to_configs
Create Date: 2026-02-28

"""
from typing import Sequence, Union
import uuid
from alembic import op
import sqlalchemy as sa


revision: str = "add_server_uuid"
down_revision: Union[str, Sequence[str], None] = "add_si_id_to_configs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("uuid", sa.String(36), nullable=True, index=True))
    # Backfill existing servers with UUIDs
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id FROM servers WHERE uuid IS NULL")).fetchall()
    for (sid,) in rows:
        u = str(uuid.uuid4())
        conn.execute(sa.text("UPDATE servers SET uuid = :u WHERE id = :id"), {"u": u, "id": sid})
    op.alter_column(
        "servers",
        "uuid",
        existing_type=sa.String(36),
        nullable=False,
    )
    op.create_unique_constraint("uq_servers_uuid", "servers", ["uuid"])


def downgrade() -> None:
    op.drop_constraint("uq_servers_uuid", "servers", type_="unique")
    op.drop_column("servers", "uuid")
