"""Add power feeds, PDUs (inlets, outlets) and server power inputs.

Existing servers get one power input named PSU1.

Revision ID: add_pdus_power_feeds
Revises: add_client_mcp_api_keys
Create Date: 2026-11-17
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_pdus_power_feeds"
down_revision: Union[str, Sequence[str], None] = "add_client_mcp_api_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    ]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "power_feeds" not in tables:
        op.create_table(
            "power_feeds",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("rack_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("voltage", sa.Integer(), nullable=False, server_default="208"),
            sa.Column("amperage", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("phase", sa.String(length=16), nullable=False, server_default="single"),
            sa.Column("max_utilisation_percent", sa.Integer(), nullable=False, server_default="80"),
            sa.Column("description", sa.Text(), nullable=True),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["rack_id"], ["racks.id"], name="fk_power_feeds_rack_id", ondelete="CASCADE"),
            sa.UniqueConstraint("rack_id", "name", name="uq_power_feeds_rack_name"),
        )
        op.create_index("ix_power_feeds_id", "power_feeds", ["id"])
        op.create_index("ix_power_feeds_rack_id", "power_feeds", ["rack_id"])

    if "pdus" not in tables:
        op.create_table(
            "pdus",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("location_id", sa.Integer(), nullable=False),
            sa.Column("rack_id", sa.Integer(), nullable=True),
            sa.Column("rack_unit", sa.Integer(), nullable=True),
            sa.Column("rack_units", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("plugin_name", sa.String(length=255), nullable=False),
            sa.Column("plugin_config", sa.JSON(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("model", sa.String(length=255), nullable=True),
            sa.Column("serial_number", sa.String(length=255), nullable=True),
            sa.Column("firmware_version", sa.String(length=255), nullable=True),
            sa.Column("inlet_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("outlet_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("outlet_metering", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("outlet_switching", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("outlet_control_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_readings", sa.JSON(), nullable=True),
            sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_poll_error", sa.Text(), nullable=True),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["location_id"], ["locations.id"], name="fk_pdus_location_id"),
            sa.ForeignKeyConstraint(["rack_id"], ["racks.id"], name="fk_pdus_rack_id"),
        )
        op.create_index("ix_pdus_id", "pdus", ["id"])
        op.create_index("ix_pdus_name", "pdus", ["name"], unique=True)
        op.create_index("ix_pdus_location_id", "pdus", ["location_id"])
        op.create_index("ix_pdus_rack_id", "pdus", ["rack_id"])
        op.create_index("ix_pdus_plugin_name", "pdus", ["plugin_name"])

    if "pdu_inlets" not in tables:
        op.create_table(
            "pdu_inlets",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("pdu_id", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("power_feed_id", sa.Integer(), nullable=True),
            sa.Column("voltage_v", sa.Float(), nullable=True),
            sa.Column("current_a", sa.Float(), nullable=True),
            sa.Column("power_w", sa.Float(), nullable=True),
            sa.Column("apparent_power_va", sa.Float(), nullable=True),
            sa.Column("energy_kwh", sa.Float(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["pdu_id"], ["pdus.id"], name="fk_pdu_inlets_pdu_id", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["power_feed_id"], ["power_feeds.id"], name="fk_pdu_inlets_power_feed_id", ondelete="SET NULL"
            ),
            sa.UniqueConstraint("pdu_id", "position", name="uq_pdu_inlets_pdu_position"),
        )
        op.create_index("ix_pdu_inlets_id", "pdu_inlets", ["id"])
        op.create_index("ix_pdu_inlets_pdu_id", "pdu_inlets", ["pdu_id"])
        op.create_index("ix_pdu_inlets_power_feed_id", "pdu_inlets", ["power_feed_id"])

    if "pdu_outlets" not in tables:
        op.create_table(
            "pdu_outlets",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("pdu_id", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("bank", sa.String(length=64), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("current_a", sa.Float(), nullable=True),
            sa.Column("power_w", sa.Float(), nullable=True),
            sa.Column("state", sa.String(length=16), nullable=True),
            sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["pdu_id"], ["pdus.id"], name="fk_pdu_outlets_pdu_id", ondelete="CASCADE"),
            sa.UniqueConstraint("pdu_id", "position", name="uq_pdu_outlets_pdu_position"),
        )
        op.create_index("ix_pdu_outlets_id", "pdu_outlets", ["id"])
        op.create_index("ix_pdu_outlets_pdu_id", "pdu_outlets", ["pdu_id"])

    if "server_power_inputs" not in tables:
        op.create_table(
            "server_power_inputs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("server_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("pdu_outlet_id", sa.Integer(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["server_id"], ["servers.id"], name="fk_server_power_inputs_server_id", ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["pdu_outlet_id"], ["pdu_outlets.id"], name="fk_server_power_inputs_pdu_outlet_id", ondelete="SET NULL"
            ),
        )
        op.create_index("ix_server_power_inputs_id", "server_power_inputs", ["id"])
        op.create_index("ix_server_power_inputs_server_id", "server_power_inputs", ["server_id"])
        op.create_index(
            "ix_server_power_inputs_pdu_outlet_id", "server_power_inputs", ["pdu_outlet_id"], unique=True
        )
        op.execute(
            "INSERT INTO server_power_inputs (server_id, name) "
            "SELECT id, 'PSU1' FROM servers"
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("server_power_inputs", "pdu_outlets", "pdu_inlets", "pdus", "power_feeds"):
        if table in tables:
            op.drop_table(table)
