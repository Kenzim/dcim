"""Add rack_units to network_switches

Revision ID: add_rack_units_switches
Revises: add_rack_units_servers
Create Date: 2025-03-07

"""
from alembic import op
import sqlalchemy as sa
from typing import Union, Sequence

revision: str = "add_rack_units_switches"
down_revision: Union[str, Sequence[str], None] = "add_rack_units_servers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "network_switches" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("network_switches")]
    if "rack_units" not in columns:
        op.add_column(
            "network_switches",
            sa.Column("rack_units", sa.Integer(), nullable=True),
        )
        op.execute(sa.text("UPDATE network_switches SET rack_units = 1 WHERE rack_units IS NULL"))
        op.alter_column(
            "network_switches",
            "rack_units",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "network_switches" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("network_switches")]
    if "rack_units" in columns:
        op.drop_column("network_switches", "rack_units")
