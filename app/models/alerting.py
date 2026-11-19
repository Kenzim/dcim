"""Alerting and notification tables: alerts, silences, channels, outbox, client preferences."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base

_FK_USERS = "users.id"
_FK_ALERTS = "alerts.id"
_SET_NULL = "SET NULL"
_CASCADE = "CASCADE"


class AlertSeverity:
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    ALL = (INFO, WARNING, CRITICAL)
    RANK = {INFO: 0, WARNING: 1, CRITICAL: 2}


class AlertState:
    OPEN = "open"
    RESOLVED = "resolved"


class AlertAudience:
    STAFF = "staff"
    CLIENT = "client"


class ChannelKind:
    EMAIL = "email"
    WEBHOOK = "webhook"
    DISCORD = "discord"
    SLACK = "slack"

    ALL = (EMAIL, WEBHOOK, DISCORD, SLACK)


class DeliveryStatus:
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"
    SKIPPED = "skipped"


class Alert(Base):
    """One occurrence of a condition. At most one open row per type and resource."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_state_audience_severity", "state", "audience", "severity"),
        Index("ix_alerts_resource", "alert_type", "resource_type", "resource_id"),
        Index("ix_alerts_resolved_at", "resolved_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    audience = Column(String(16), nullable=False, default=AlertAudience.STAFF, server_default="staff")
    owner_user_id = Column(
        Integer, ForeignKey(_FK_USERS, ondelete=_CASCADE), nullable=True, index=True
    )
    alert_type = Column(String(64), nullable=False, index=True)
    resource_type = Column(String(64), nullable=False)
    resource_id = Column(String(128), nullable=False)
    resource_label = Column(String(255), nullable=True)
    severity = Column(String(16), nullable=False)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=True)
    details = Column(JSON, nullable=False, default=dict)
    state = Column(String(16), nullable=False, default=AlertState.OPEN, server_default="open")
    # Set only while open, so the unique index allows one open alert per key.
    active_key = Column(String(255), nullable=True, unique=True)
    opened_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    clear_since = Column(DateTime(timezone=True), nullable=True)
    notified_at = Column(DateTime(timezone=True), nullable=True)
    last_notified_at = Column(DateTime(timezone=True), nullable=True)
    notify_count = Column(Integer, nullable=False, default=0, server_default="0")
    resolve_notice_due_at = Column(DateTime(timezone=True), nullable=True)
    resolve_notified = Column(Boolean, nullable=False, default=False, server_default="0")
    flap_count = Column(Integer, nullable=False, default=0, server_default="0")
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_by = Column(
        Integer, ForeignKey(_FK_USERS, ondelete=_SET_NULL), nullable=True
    )
    acknowledge_note = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    acknowledged_by_user = relationship("User", foreign_keys=[acknowledged_by])


class AlertSilence(Base):
    """Mutes notices for a type, a resource, or both, for a period."""

    __tablename__ = "alert_silences"
    __table_args__ = (Index("ix_alert_silences_window", "starts_at", "ends_at"),)

    id = Column(Integer, primary_key=True, index=True)
    alert_type = Column(String(64), nullable=True, index=True)
    resource_type = Column(String(64), nullable=True)
    resource_id = Column(String(128), nullable=True)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String(512), nullable=True)
    created_by = Column(Integer, ForeignKey(_FK_USERS, ondelete=_SET_NULL), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    created_by_user = relationship("User", foreign_keys=[created_by])


class AlertTypeSetting(Base):
    """Per-type overrides of the defaults in the alert catalogue."""

    __tablename__ = "alert_type_settings"

    alert_type = Column(String(64), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    severity = Column(String(16), nullable=True)
    params = Column(JSON, nullable=False, default=dict)
    reminder_interval_seconds = Column(Integer, nullable=True)
    hold_down_seconds = Column(Integer, nullable=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AlertChannel(Base):
    """Where staff alerts go: an email list, a generic webhook, Discord or Slack."""

    __tablename__ = "alert_channels"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False, unique=True)
    kind = Column(String(16), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    min_severity = Column(String(16), nullable=False, default=AlertSeverity.WARNING, server_default="warning")
    alert_types = Column(JSON, nullable=False, default=list)
    send_resolved = Column(Boolean, nullable=False, default=True, server_default="1")
    # email: {"addresses": [...]}; others: {"url": sealed, "secret": sealed}.
    config = Column(JSON, nullable=False, default=dict)
    created_by = Column(Integer, ForeignKey(_FK_USERS, ondelete=_SET_NULL), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AlertRecipient(Base):
    """A staff user who receives alert email at their account address."""

    __tablename__ = "alert_recipients"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey(_FK_USERS, ondelete=_CASCADE), nullable=False, unique=True
    )
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    min_severity = Column(String(16), nullable=False, default=AlertSeverity.WARNING, server_default="warning")
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User", foreign_keys=[user_id])


class NotificationDelivery(Base):
    """Outbox row for one message to one target (staff alert or client notice)."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_notification_deliveries_attempts"),
        Index("ix_notification_deliveries_status_attempt", "status", "next_attempt_at"),
        Index("ix_notification_deliveries_claim", "claim_expires_at", "claim_token"),
        Index("ix_notification_deliveries_user_created", "user_id", "channel_kind", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    audience = Column(String(16), nullable=False, default=AlertAudience.STAFF, server_default="staff")
    alert_id = Column(
        Integer, ForeignKey(_FK_ALERTS, ondelete=_SET_NULL), nullable=True, index=True
    )
    user_id = Column(Integer, ForeignKey(_FK_USERS, ondelete=_CASCADE), nullable=True)
    channel_id = Column(
        Integer, ForeignKey("alert_channels.id", ondelete=_SET_NULL), nullable=True
    )
    channel_kind = Column(String(16), nullable=False)
    target = Column(String(512), nullable=False)
    event = Column(String(32), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default=DeliveryStatus.QUEUED, server_default="queued", index=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    claim_token = Column(String(128), nullable=True)
    claim_expires_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ClientNotificationPref(Base):
    """A client's opt-in choices for notices about their own account and services."""

    __tablename__ = "client_notification_prefs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey(_FK_USERS, ondelete=_CASCADE), nullable=False, unique=True
    )
    email_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    categories = Column(JSON, nullable=False, default=dict)
    webhook_url = Column(Text, nullable=True)
    webhook_secret = Column(Text, nullable=True)
    webhook_error = Column(String(255), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User", foreign_keys=[user_id])


class VmBackupRun(Base):
    """A Proxmox backup started through Rackflow, followed until it finishes."""

    __tablename__ = "vm_backup_runs"
    __table_args__ = (Index("ix_vm_backup_runs_status", "status", "started_at"),)

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(
        Integer, ForeignKey("services.id", ondelete=_CASCADE), nullable=False, index=True
    )
    upid = Column(String(255), nullable=False)
    status = Column(String(16), nullable=False, default="running", server_default="running")
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    notified = Column(Boolean, nullable=False, default=False, server_default="0")
