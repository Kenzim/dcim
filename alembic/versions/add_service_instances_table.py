"""Add service_instances table for per-location DHCP/TFTP runners

Revision ID: add_service_instances
Revises: add_dhcp_tftp_config
Create Date: 2026-02-26

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "add_service_instances"
down_revision: Union[str, Sequence[str], None] = "add_dhcp_tftp_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "service_instances",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("service_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("last_connection_test", sa.DateTime(), nullable=True),
        sa.Column("connection_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_service_instances_location_id", "service_instances", ["location_id"], unique=False)
    op.create_index("ix_service_instances_service_type", "service_instances", ["service_type"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_service_instances_service_type", table_name="service_instances")
    op.drop_index("ix_service_instances_location_id", table_name="service_instances")
    op.drop_table("service_instances")
