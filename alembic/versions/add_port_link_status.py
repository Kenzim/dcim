"""Add stored link status timestamps to switch ports and server network ports

Revision ID: add_port_link_status
Revises: add_location_proxy_port

switch_ports.status_updated_at records when admin_status/oper_status were last
written by the SNMP poller. network_ports.link_up/link_checked_at hold the NIC
carrier state reported by the last hardware detection run.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "add_port_link_status"
down_revision: Union[str, Sequence[str], None] = "add_location_proxy_port"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("switch_ports", sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("network_ports", sa.Column("link_up", sa.Boolean(), nullable=True))
    op.add_column("network_ports", sa.Column("link_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("network_ports", "link_checked_at")
    op.drop_column("network_ports", "link_up")
    op.drop_column("switch_ports", "status_updated_at")
