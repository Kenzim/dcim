"""Private network sharing: members, invitations, member limit and owner-gone marker.

Revision ID: add_private_network_sharing
Revises: alerting_tables
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "add_private_network_sharing"
down_revision: Union[str, Sequence[str], None] = "alerting_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "private_network_pools",
        sa.Column("max_members_per_network", sa.Integer(), nullable=False, server_default="5"),
    )
    op.add_column("private_networks", sa.Column("owner_gone_since", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "private_network_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "network_id", sa.Integer(), sa.ForeignKey("private_networks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invited_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("network_id", "user_id", name="uq_private_network_member"),
    )
    op.create_index("ix_private_network_members_id", "private_network_members", ["id"])
    op.create_index("ix_private_network_members_network_id", "private_network_members", ["network_id"])
    op.create_index("ix_private_network_members_user_id", "private_network_members", ["user_id"])

    op.create_table(
        "private_network_invitations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "network_id", sa.Integer(), sa.ForeignKey("private_networks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("inviter_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("invitee_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("invitee_label", sa.String(255), nullable=False),
        sa.Column("status", sa.String(9), nullable=False, server_default="pending"),
        sa.Column("active_key", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("active_key", name="uq_private_network_invitation_active"),
    )
    op.create_index("ix_private_network_invitations_id", "private_network_invitations", ["id"])
    op.create_index("ix_private_network_invitations_network_id", "private_network_invitations", ["network_id"])
    op.create_index(
        "ix_private_network_invitations_invitee_user_id", "private_network_invitations", ["invitee_user_id"]
    )
    op.create_index("ix_private_network_invitations_status", "private_network_invitations", ["status"])


def downgrade() -> None:
    op.drop_table("private_network_invitations")
    op.drop_table("private_network_members")
    op.drop_column("private_networks", "owner_gone_since")
    op.drop_column("private_network_pools", "max_members_per_network")
