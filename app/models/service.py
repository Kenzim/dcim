from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Text, JSON, Boolean, Enum as SQLEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base
import enum


class ServiceStatus(str, enum.Enum):
    """Service status options"""
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"
    PENDING = "pending"


class Service(Base):
    """
    Service model - represents a service provisioned by an external billing system.
    
    Services are what external billing systems (WHMCS, etc.) see and manage.
    Each service links to a server, and the server links to an external user.
    """
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)  # Service name/identifier
    external_service_id = Column(String(255), nullable=True, index=True)  # Service ID in external system (optional)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False, index=True)
    external_user_id = Column(Integer, ForeignKey("external_users.id"), nullable=False, index=True)
    status = Column(SQLEnum(ServiceStatus, native_enum=False, values_callable=lambda x: [e.value for e in x]), nullable=False, default=ServiceStatus.PENDING)
    description = Column(Text, nullable=True)
    config = Column(JSON, nullable=True)  # Service-specific configuration/metadata
    
    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    terminated_at = Column(DateTime(timezone=True), nullable=True)  # When service was terminated
    
    # Relationships
    server = relationship("Server", backref="services")
    external_user = relationship("ExternalUser", backref="services")

    def __repr__(self):
        return f"<Service(id={self.id}, name='{self.name}', server_id={self.server_id}, status='{self.status.value}')>"
