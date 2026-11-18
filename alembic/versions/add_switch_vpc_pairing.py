"""Add Cisco vPC pairing columns on network_switches

Revision ID: add_switch_vpc_pairing
Revises: add_nfs_storage
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "add_switch_vpc_pairing"
down_revision: Union[str, Sequence[str], None] = "add_nfs_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    ("vpc_domain_id", sa.Integer()),
    ("vpc_peer_switch_id", sa.Integer()),
    ("vpc_peer_link", sa.String(64)),
    ("vpc_role", sa.String(64)),
    ("vpc_detected_at", sa.DateTime(timezone=True)),
    ("vpc_error", sa.Text()),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_columns("network_switches")}
    for name, type_ in _COLUMNS:
        if name not in existing:
            op.add_column("network_switches", sa.Column(name, type_, nullable=True))
    foreign_keys = {fk.get("name") for fk in inspector.get_foreign_keys("network_switches")}
    if "fk_network_switches_vpc_peer" in foreign_keys:
        return
    op.create_foreign_key(
        "fk_network_switches_vpc_peer",
        "network_switches",
        "network_switches",
        ["vpc_peer_switch_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_network_switches_vpc_peer", "network_switches", type_="foreignkey")
    op.drop_column("network_switches", "vpc_error")
    op.drop_column("network_switches", "vpc_detected_at")
    op.drop_column("network_switches", "vpc_role")
    op.drop_column("network_switches", "vpc_peer_link")
    op.drop_column("network_switches", "vpc_peer_switch_id")
    op.drop_column("network_switches", "vpc_domain_id")
