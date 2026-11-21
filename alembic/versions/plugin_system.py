"""Plugin system tables

Installations, settings, key-value state, per-service links, the event outbox,
schedules and logs. See docs/plugin-system.md.

Revision ID: plugin_system
Revises: private_network_dhcp
Create Date: 2026-11-21 18:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "plugin_system"
down_revision: Union[str, Sequence[str], None] = "private_network_dhcp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _now():
    return sa.func.now()


def _tables():
    return {
        "plugin_installations": lambda: op.create_table(
            "plugin_installations",
            sa.Column("id", sa.String(48), primary_key=True),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("version", sa.String(32), nullable=False),
            sa.Column("manifest", sa.JSON(), nullable=False),
            sa.Column("directory", sa.String(255), nullable=False),
            sa.Column("code_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("granted_permissions", sa.JSON(), nullable=False),
            sa.Column("addon_refs", sa.JSON(), nullable=False),
            sa.Column("disabled_reason", sa.String(500), nullable=True),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("circuit_opens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("circuit_open_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        ),
        "plugin_settings": lambda: op.create_table(
            "plugin_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plugin_id", sa.String(48), nullable=False, index=True),
            sa.Column("key", sa.String(64), nullable=False),
            sa.Column("value", sa.JSON(), nullable=True),
            sa.Column("sealed_value", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
            sa.UniqueConstraint("plugin_id", "key", name="uq_plugin_settings_plugin_key"),
        ),
        "plugin_state": lambda: op.create_table(
            "plugin_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plugin_id", sa.String(48), nullable=False),
            sa.Column("scope", sa.String(32), nullable=False),
            sa.Column("key", sa.String(128), nullable=False),
            sa.Column(
                "service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=True, index=True
            ),
            sa.Column("value", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
            sa.UniqueConstraint("plugin_id", "scope", "key", name="uq_plugin_state_scope_key"),
        ),
        "plugin_service_links": lambda: op.create_table(
            "plugin_service_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plugin_id", sa.String(48), nullable=False, index=True),
            sa.Column(
                "service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("addon_ref", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
            sa.UniqueConstraint("plugin_id", "service_id", name="uq_plugin_service_links"),
        ),
        "plugin_deliveries": lambda: op.create_table(
            "plugin_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.String(36), nullable=False),
            sa.Column("plugin_id", sa.String(48), nullable=False, index=True),
            sa.Column("event", sa.String(64), nullable=False),
            sa.Column("service_id", sa.Integer(), nullable=True, index=True),
            sa.Column("envelope", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claim_token", sa.String(36), nullable=True),
            sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("event_id", "plugin_id", name="uq_plugin_deliveries_event_plugin"),
            sa.Index("ix_plugin_deliveries_due", "status", "next_attempt_at"),
        ),
        "plugin_schedules": lambda: op.create_table(
            "plugin_schedules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plugin_id", sa.String(48), nullable=False, index=True),
            sa.Column("schedule", sa.String(64), nullable=False),
            sa.Column("scope", sa.String(32), nullable=False),
            sa.Column(
                "service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=True, index=True
            ),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_status", sa.String(16), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("lease_owner", sa.String(96), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("plugin_id", "schedule", "scope", name="uq_plugin_schedules"),
        ),
        "plugin_logs": lambda: op.create_table(
            "plugin_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plugin_id", sa.String(48), nullable=False),
            sa.Column("level", sa.String(8), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("service_id", sa.Integer(), nullable=True),
            sa.Column("context", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
            sa.Index("ix_plugin_logs_plugin_created", "plugin_id", "created_at"),
        ),
    }


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for name, create in _tables().items():
        if name not in existing:
            create()


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for name in reversed(list(_tables())):
        if name in existing:
            op.drop_table(name)
