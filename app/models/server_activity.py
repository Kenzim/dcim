from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum as SQLEnum, JSON, CheckConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base
import enum


class ServerActivityEventType(str, enum.Enum):
    """Activity event categories for server timeline entries."""

    SERVICE = "service"
    POWER = "power"
    INSTALL = "install"


class ServerActivityStatus(str, enum.Enum):
    """Lifecycle state of a logged activity event."""

    ATTEMPT = "attempt"
    SUCCESS = "success"
    FAILED = "failed"


class ServerActivity(Base):
    """Append-only activity log: either tied to a Server (bare metal) or a Service (VM)."""

    __tablename__ = "server_activity"
    __table_args__ = (
        CheckConstraint(
            "(server_id IS NOT NULL AND service_id IS NULL) OR (server_id IS NULL AND service_id IS NOT NULL)",
            name="ck_server_activity_server_xor_service",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=True, index=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=True, index=True)
    event_type = Column(
        SQLEnum(
            ServerActivityEventType,
            native_enum=False,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    action = Column(String(100), nullable=False)
    status = Column(
        SQLEnum(
            ServerActivityStatus,
            native_enum=False,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    message = Column(String(512), nullable=False)
    source = Column(String(100), nullable=False)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    server = relationship("Server", backref="activity_log_entries")
    service = relationship("Service", backref="activity_log_entries")

    def __repr__(self):
        return (
            "ServerActivity("
            f"id={self.id}, server_id={self.server_id}, service_id={self.service_id}, "
            f"event_type={self.event_type.value}, action='{self.action}', status={self.status.value})"
        )
