from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Text, JSON, Enum as SQLEnum
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


class ServiceType(str, enum.Enum):
    """High-level service type used for provisioning and lifecycle routing."""

    BARE_METAL = "bare_metal"
    VM = "vm"
    HTTP_PROXY = "http_proxy"


class ProvisioningSource(str, enum.Enum):
    """Where the service record was created from (billing integration vs internal admin)."""

    BILLING = "billing"
    INTERNAL = "internal"


class Service(Base):
    """
    Billing / catalog parent row (WHMCS line item, internal test VM, etc.).

    Type-specific data lives in ``service_bare_metal`` or ``service_vm``.
    """

    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    external_service_id = Column(String(255), nullable=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    external_user_id = Column(Integer, ForeignKey("external_users.id"), nullable=True, index=True)
    service_type = Column(
        SQLEnum(ServiceType, native_enum=False, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=ServiceType.BARE_METAL,
        index=True,
    )
    status = Column(
        SQLEnum(ServiceStatus, native_enum=False, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=ServiceStatus.PENDING,
    )
    description = Column(Text, nullable=True)
    config = Column(JSON, nullable=True)
    product_code = Column(String(128), nullable=True, index=True)
    os_code = Column(String(128), nullable=True, index=True)
    product_snapshot = Column(JSON, nullable=True)

    provisioning_source = Column(
        SQLEnum(ProvisioningSource, native_enum=False, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=ProvisioningSource.BILLING,
        server_default=ProvisioningSource.BILLING.value,
        index=True,
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    terminated_at = Column(DateTime(timezone=True), nullable=True)

    owner_user = relationship("User", backref="owned_services")
    external_user = relationship("ExternalUser", backref="services")
    bare_metal = relationship(
        "ServiceBareMetal",
        back_populates="service",
        uselist=False,
        cascade="all, delete-orphan",
    )
    vm = relationship(
        "ServiceVm",
        back_populates="service",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return (
            f"<Service(id={self.id}, name='{self.name}', type='{self.service_type.value}', "
            f"status='{self.status.value}')>"
        )
