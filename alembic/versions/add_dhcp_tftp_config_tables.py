"""Add dhcp_config and tftp_config tables for DB-driven service config

Revision ID: add_dhcp_tftp_config
Revises: add_srvgrp_permitted
Create Date: 2026-02-20

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "add_dhcp_tftp_config"
down_revision: Union[str, Sequence[str], None] = "add_srvgrp_permitted"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dhcp_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("interfaces", sa.JSON(), nullable=False),
        sa.Column("dns_servers", sa.JSON(), nullable=True),
        sa.Column("hand_out_leases", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("default_lease_time", sa.Integer(), nullable=False, server_default=sa.text("3600")),
        sa.Column("max_lease_time", sa.Integer(), nullable=False, server_default=sa.text("7200")),
        sa.Column("config_file_path", sa.String(512), nullable=False),
        sa.Column("lease_file_path", sa.String(512), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dhcp_config_id"), "dhcp_config", ["id"], unique=False)

    op.create_table(
        "tftp_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("root_directory", sa.String(512), nullable=False),
        sa.Column("bind_address", sa.String(128), nullable=False),
        sa.Column("bind_port", sa.Integer(), nullable=False, server_default=sa.text("69")),
        sa.Column("allow_create", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("verbose", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("ipv4_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tftp_config_id"), "tftp_config", ["id"], unique=False)

    # Insert default row for dhcp_config (paths from env at runtime; defaults here for migration)
    op.execute(
        sa.text("""
        INSERT INTO dhcp_config (id, enabled, interfaces, hand_out_leases, default_lease_time, max_lease_time, config_file_path, lease_file_path)
        VALUES (1, 1, '[{"interface": "eth1", "ip": "192.168.12.74"}]', 1, 3600, 7200, '/shared/dhcp/dhcpd.conf', '/shared/dhcp/dhcpd.leases')
        """)
    )
    # Insert default row for tftp_config
    op.execute(
        sa.text("""
        INSERT INTO tftp_config (id, enabled, root_directory, bind_address, bind_port, allow_create, verbose, ipv4_only)
        VALUES (1, 1, '/shared/tftp', '0.0.0.0', 69, 1, 1, 1)
        """)
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tftp_config_id"), table_name="tftp_config")
    op.drop_table("tftp_config")
    op.drop_index(op.f("ix_dhcp_config_id"), table_name="dhcp_config")
    op.drop_table("dhcp_config")
