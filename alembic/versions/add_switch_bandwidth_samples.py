"""add_switch_bandwidth_samples

Revision ID: add_bandwidth_samples
Revises: remove_mgmt_ip
Create Date: 2026-02-09

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "add_bandwidth_samples"
down_revision: Union[str, Sequence[str], None] = "remove_mgmt_ip"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "switch_bandwidth_samples",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("switch_id", sa.Integer(), nullable=False),
        sa.Column("port_identifier", sa.String(length=255), nullable=False),
        sa.Column("if_index", sa.Integer(), nullable=True),
        sa.Column("sampled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("bytes_in", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes_out", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("in_errors", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("out_errors", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("in_discards", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("out_discards", sa.BigInteger(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["switch_id"], ["network_switches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_switch_bandwidth_samples_id"), "switch_bandwidth_samples", ["id"], unique=False)
    op.create_index(op.f("ix_switch_bandwidth_samples_switch_id"), "switch_bandwidth_samples", ["switch_id"], unique=False)
    op.create_index(op.f("ix_switch_bandwidth_samples_port_identifier"), "switch_bandwidth_samples", ["port_identifier"], unique=False)
    op.create_index(op.f("ix_switch_bandwidth_samples_sampled_at"), "switch_bandwidth_samples", ["sampled_at"], unique=False)
    # Composite index for querying by switch + time range
    op.create_index(
        "ix_switch_bandwidth_samples_switch_sampled",
        "switch_bandwidth_samples",
        ["switch_id", "sampled_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_switch_bandwidth_samples_switch_sampled", table_name="switch_bandwidth_samples")
    op.drop_index(op.f("ix_switch_bandwidth_samples_sampled_at"), table_name="switch_bandwidth_samples")
    op.drop_index(op.f("ix_switch_bandwidth_samples_port_identifier"), table_name="switch_bandwidth_samples")
    op.drop_index(op.f("ix_switch_bandwidth_samples_switch_id"), table_name="switch_bandwidth_samples")
    op.drop_index(op.f("ix_switch_bandwidth_samples_id"), table_name="switch_bandwidth_samples")
    op.drop_table("switch_bandwidth_samples")
