"""Alerting and notification tables.

Adds alerts, silences, per-type settings, channels, staff recipients, the
notification delivery outbox, client notification preferences and the VM
backup run record.

Revision ID: alerting_tables
Revises: drop_proxy_access_requires_product
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "alerting_tables"
down_revision: Union[str, Sequence[str], None] = "drop_proxy_access_requires_product"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "vm_backup_runs",
    "client_notification_prefs",
    "notification_deliveries",
    "alert_recipients",
    "alert_channels",
    "alert_type_settings",
    "alert_silences",
    "alerts",
)


def _now():
    return sa.func.now()


def _stamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    ]


def _updated():
    return sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False)


def _create_alerts() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("audience", sa.String(16), nullable=False, server_default="staff"),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("alert_type", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("resource_label", sa.String(255), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("active_key", sa.String(255), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clear_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notify_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolve_notice_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolve_notified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("flap_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("acknowledge_note", sa.String(512), nullable=True),
        *_stamps(),
        _updated(),
        sa.UniqueConstraint("active_key", name="uq_alerts_active_key"),
    )
    op.create_index("ix_alerts_id", "alerts", ["id"])
    op.create_index("ix_alerts_owner_user_id", "alerts", ["owner_user_id"])
    op.create_index("ix_alerts_alert_type", "alerts", ["alert_type"])
    op.create_index("ix_alerts_state_audience_severity", "alerts", ["state", "audience", "severity"])
    op.create_index("ix_alerts_resource", "alerts", ["alert_type", "resource_type", "resource_id"])
    op.create_index("ix_alerts_resolved_at", "alerts", ["resolved_at"])


def _create_config_tables() -> None:
    op.create_table(
        "alert_silences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alert_type", sa.String(64), nullable=True),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", sa.String(128), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(512), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_alert_silences_id", "alert_silences", ["id"])
    op.create_index("ix_alert_silences_alert_type", "alert_silences", ["alert_type"])
    op.create_index("ix_alert_silences_window", "alert_silences", ["starts_at", "ends_at"])

    op.create_table(
        "alert_type_settings",
        sa.Column("alert_type", sa.String(64), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("severity", sa.String(16), nullable=True),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("reminder_interval_seconds", sa.Integer(), nullable=True),
        sa.Column("hold_down_seconds", sa.Integer(), nullable=True),
        _updated(),
    )

    op.create_table(
        "alert_channels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("min_severity", sa.String(16), nullable=False, server_default="warning"),
        sa.Column("alert_types", sa.JSON(), nullable=False),
        sa.Column("send_resolved", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
        _updated(),
        sa.UniqueConstraint("name", name="uq_alert_channels_name"),
    )
    op.create_index("ix_alert_channels_id", "alert_channels", ["id"])

    op.create_table(
        "alert_recipients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("min_severity", sa.String(16), nullable=False, server_default="warning"),
        _updated(),
        sa.UniqueConstraint("user_id", name="uq_alert_recipients_user_id"),
    )
    op.create_index("ix_alert_recipients_id", "alert_recipients", ["id"])


def _create_delivery_tables() -> None:
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False, server_default="staff"),
        sa.Column("alert_id", sa.Integer(), sa.ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("alert_channels.id", ondelete="SET NULL"), nullable=True),
        sa.Column("channel_kind", sa.String(16), nullable=False),
        sa.Column("target", sa.String(512), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claim_token", sa.String(128), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        _updated(),
        sa.UniqueConstraint("idempotency_key", name="uq_notification_deliveries_idempotency_key"),
        sa.CheckConstraint("attempts >= 0", name="ck_notification_deliveries_attempts"),
    )
    op.create_index("ix_notification_deliveries_id", "notification_deliveries", ["id"])
    op.create_index("ix_notification_deliveries_alert_id", "notification_deliveries", ["alert_id"])
    op.create_index("ix_notification_deliveries_status", "notification_deliveries", ["status"])
    op.create_index("ix_notification_deliveries_created_at", "notification_deliveries", ["created_at"])
    op.create_index(
        "ix_notification_deliveries_status_attempt", "notification_deliveries", ["status", "next_attempt_at"]
    )
    op.create_index("ix_notification_deliveries_claim", "notification_deliveries", ["claim_expires_at", "claim_token"])
    op.create_index(
        "ix_notification_deliveries_user_created",
        "notification_deliveries",
        ["user_id", "channel_kind", "created_at"],
    )

    op.create_table(
        "client_notification_prefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("webhook_secret", sa.Text(), nullable=True),
        sa.Column("webhook_error", sa.String(255), nullable=True),
        _updated(),
        sa.UniqueConstraint("user_id", name="uq_client_notification_prefs_user_id"),
    )
    op.create_index("ix_client_notification_prefs_id", "client_notification_prefs", ["id"])

    op.create_table(
        "vm_backup_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("upid", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_vm_backup_runs_id", "vm_backup_runs", ["id"])
    op.create_index("ix_vm_backup_runs_service_id", "vm_backup_runs", ["service_id"])
    op.create_index("ix_vm_backup_runs_status", "vm_backup_runs", ["status", "started_at"])


def upgrade() -> None:
    present = set(sa.inspect(op.get_bind()).get_table_names())
    if "alerts" not in present:
        _create_alerts()
    if "alert_channels" not in present:
        _create_config_tables()
    if "notification_deliveries" not in present:
        _create_delivery_tables()


def downgrade() -> None:
    present = set(sa.inspect(op.get_bind()).get_table_names())
    for table in _TABLES:
        if table in present:
            op.drop_table(table)
