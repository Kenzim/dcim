"""Add service_instance_id to dhcp_config and tftp_config for per-location config

Revision ID: add_si_id_to_configs
Revises: add_service_instances
Create Date: 2026-02-26

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "add_si_id_to_configs"
down_revision: Union[str, Sequence[str], None] = "add_service_instances"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "dhcp_config",
        "id",
        existing_type=sa.Integer(),
        autoincrement=True,
    )
    op.add_column("dhcp_config", sa.Column("service_instance_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_dhcp_config_service_instance",
        "dhcp_config",
        "service_instances",
        ["service_instance_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_dhcp_config_service_instance_id", "dhcp_config", ["service_instance_id"], unique=True)

    op.alter_column(
        "tftp_config",
        "id",
        existing_type=sa.Integer(),
        autoincrement=True,
    )
    op.add_column("tftp_config", sa.Column("service_instance_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_tftp_config_service_instance",
        "tftp_config",
        "service_instances",
        ["service_instance_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_tftp_config_service_instance_id", "tftp_config", ["service_instance_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tftp_config_service_instance_id", table_name="tftp_config")
    op.drop_constraint("fk_tftp_config_service_instance", "tftp_config", type_="foreignkey")
    op.drop_column("tftp_config", "service_instance_id")

    op.drop_index("ix_dhcp_config_service_instance_id", table_name="dhcp_config")
    op.drop_constraint("fk_dhcp_config_service_instance", "dhcp_config", type_="foreignkey")
    op.drop_column("dhcp_config", "service_instance_id")
