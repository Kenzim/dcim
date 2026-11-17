"""Add switch_lldp_neighbors and LLDP collection status on network_switches

Revision ID: add_switch_lldp_neighbors
Revises: add_port_link_status
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "add_switch_lldp_neighbors"
down_revision: Union[str, Sequence[str], None] = "add_port_link_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "switch_lldp_neighbors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("switch_id", sa.Integer(), sa.ForeignKey("network_switches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("switch_port_id", sa.Integer(), sa.ForeignKey("switch_ports.id", ondelete="SET NULL"), nullable=True),
        sa.Column("local_port_num", sa.Integer(), nullable=True),
        sa.Column("local_port_label", sa.String(255), nullable=True),
        sa.Column("chassis_id_subtype", sa.Integer(), nullable=True),
        sa.Column("chassis_id", sa.String(255), nullable=True),
        sa.Column("port_id_subtype", sa.Integer(), nullable=True),
        sa.Column("port_id", sa.String(255), nullable=True),
        sa.Column("port_desc", sa.String(255), nullable=True),
        sa.Column("sys_name", sa.String(255), nullable=True),
        sa.Column("sys_desc", sa.Text(), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_switch_lldp_neighbors_id", "switch_lldp_neighbors", ["id"])
    op.create_index("ix_switch_lldp_neighbors_switch_id", "switch_lldp_neighbors", ["switch_id"])
    op.create_index("ix_switch_lldp_neighbors_switch_port_id", "switch_lldp_neighbors", ["switch_port_id"])
    op.create_index("ix_switch_lldp_neighbors_chassis_id", "switch_lldp_neighbors", ["chassis_id"])
    op.create_index("ix_switch_lldp_neighbors_port_id", "switch_lldp_neighbors", ["port_id"])
    op.add_column("network_switches", sa.Column("lldp_collected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("network_switches", sa.Column("lldp_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("network_switches", "lldp_error")
    op.drop_column("network_switches", "lldp_collected_at")
    op.drop_index("ix_switch_lldp_neighbors_port_id", table_name="switch_lldp_neighbors")
    op.drop_index("ix_switch_lldp_neighbors_chassis_id", table_name="switch_lldp_neighbors")
    op.drop_index("ix_switch_lldp_neighbors_switch_port_id", table_name="switch_lldp_neighbors")
    op.drop_index("ix_switch_lldp_neighbors_switch_id", table_name="switch_lldp_neighbors")
    op.drop_index("ix_switch_lldp_neighbors_id", table_name="switch_lldp_neighbors")
    op.drop_table("switch_lldp_neighbors")
