"""Tables of the plugin system (docs/plugin-system.md section 15)."""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.core.database import Base


class PluginStatus:
    ENABLED = "enabled"
    DISABLED = "disabled"
    FAILED = "failed"
    FILES_CHANGED = "files_changed"


class DeliveryStatus:
    QUEUED = "queued"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD = "dead"
    SKIPPED = "skipped"


class PluginInstallation(Base):
    __tablename__ = "plugin_installations"

    id = Column(String(48), primary_key=True)
    name = Column(String(80), nullable=False)
    version = Column(String(32), nullable=False)
    manifest = Column(JSON, nullable=False)
    directory = Column(String(255), nullable=False)
    code_hash = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default=PluginStatus.DISABLED)
    granted_permissions = Column(JSON, nullable=False, default=list)
    addon_refs = Column(JSON, nullable=False, default=list)
    disabled_reason = Column(String(500), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0, server_default="0")
    total_failures = Column(Integer, nullable=False, default=0, server_default="0")
    circuit_opens = Column(Integer, nullable=False, default=0, server_default="0")
    circuit_open_until = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    last_error_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    # Highest supported plugin API version the manifest's range allows (API 1.1,
    # docs/billing-phase2-design.md 8.1); NULL until the loader records it.
    effective_api = Column(String(16), nullable=True)
    installed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PluginSetting(Base):
    __tablename__ = "plugin_settings"
    __table_args__ = (UniqueConstraint("plugin_id", "key", name="uq_plugin_settings_plugin_key"),)

    id = Column(Integer, primary_key=True)
    plugin_id = Column(String(48), nullable=False, index=True)
    key = Column(String(64), nullable=False)
    value = Column(JSON, nullable=True)
    sealed_value = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PluginState(Base):
    __tablename__ = "plugin_state"
    __table_args__ = (UniqueConstraint("plugin_id", "scope", "key", name="uq_plugin_state_scope_key"),)

    id = Column(Integer, primary_key=True)
    plugin_id = Column(String(48), nullable=False)
    scope = Column(String(32), nullable=False)
    key = Column(String(128), nullable=False)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=True, index=True)
    value = Column(JSON, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PluginServiceLink(Base):
    __tablename__ = "plugin_service_links"
    __table_args__ = (UniqueConstraint("plugin_id", "service_id", name="uq_plugin_service_links"),)

    id = Column(Integer, primary_key=True)
    plugin_id = Column(String(48), nullable=False, index=True)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True)
    enabled = Column(Boolean, nullable=False)
    source = Column(String(16), nullable=False)
    addon_ref = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PluginDelivery(Base):
    __tablename__ = "plugin_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "plugin_id", name="uq_plugin_deliveries_event_plugin"),
        Index("ix_plugin_deliveries_due", "status", "next_attempt_at"),
    )

    id = Column(Integer, primary_key=True)
    event_id = Column(String(36), nullable=False)
    plugin_id = Column(String(48), nullable=False, index=True)
    event = Column(String(64), nullable=False)
    service_id = Column(Integer, nullable=True, index=True)
    envelope = Column(JSON, nullable=False)
    status = Column(String(16), nullable=False, default=DeliveryStatus.QUEUED)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    claim_token = Column(String(36), nullable=True)
    claim_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    delivered_at = Column(DateTime(timezone=True), nullable=True)


class PluginSchedule(Base):
    __tablename__ = "plugin_schedules"
    __table_args__ = (UniqueConstraint("plugin_id", "schedule", "scope", name="uq_plugin_schedules"),)

    id = Column(Integer, primary_key=True)
    plugin_id = Column(String(48), nullable=False, index=True)
    schedule = Column(String(64), nullable=False)
    scope = Column(String(32), nullable=False)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=True, index=True)
    next_run_at = Column(DateTime(timezone=True), nullable=False)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_status = Column(String(16), nullable=True)
    last_error = Column(Text, nullable=True)
    lease_owner = Column(String(96), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)


class PluginLog(Base):
    __tablename__ = "plugin_logs"
    __table_args__ = (Index("ix_plugin_logs_plugin_created", "plugin_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    plugin_id = Column(String(48), nullable=False)
    level = Column(String(8), nullable=False)
    message = Column(Text, nullable=False)
    service_id = Column(Integer, nullable=True)
    context = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
