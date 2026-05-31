from sqlalchemy import Column, Integer, String, DateTime, Boolean, JSON, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class IPSubnet(Base):
    __tablename__ = "ip_subnets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    cidr = Column(String(64), nullable=False, unique=True, index=True)
    location_id = Column(Integer, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True, index=True)
    range_start = Column(String(64), nullable=True)
    range_end = Column(String(64), nullable=True)
    tags = Column(JSON, nullable=False, default=list)
    allocation_strategy = Column(String(64), nullable=False, default="first_free")
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    ip_addresses = relationship("IPAddress", back_populates="subnet", cascade="all, delete-orphan")


class IPAddress(Base):
    __tablename__ = "ip_addresses"

    id = Column(Integer, primary_key=True, index=True)
    subnet_id = Column(Integer, ForeignKey("ip_subnets.id", ondelete="CASCADE"), nullable=False, index=True)
    ip_address = Column(String(64), nullable=False, unique=True, index=True)
    state = Column(String(32), nullable=False, default="free", index=True)
    details = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    subnet = relationship("IPSubnet", back_populates="ip_addresses")
    assignment = relationship("ServiceIPAssignment", back_populates="ip", uselist=False)


class ServiceIPAssignment(Base):
    __tablename__ = "service_ip_assignments"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True)
    ip_id = Column(Integer, ForeignKey("ip_addresses.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    username = Column(String(255), nullable=True)
    password = Column(String(255), nullable=True)
    assigned_by = Column(String(128), nullable=True)
    assigned_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ip = relationship("IPAddress", back_populates="assignment")
    service = relationship("Service")


class ServiceIPAssignmentHistory(Base):
    __tablename__ = "service_ip_assignment_history"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="SET NULL"), nullable=True, index=True)
    ip_address = Column(String(64), nullable=False, index=True)
    subnet_cidr = Column(String(64), nullable=True)
    action = Column(String(32), nullable=False, index=True)  # assigned | released | reconciled
    username = Column(String(255), nullable=True)
    assigned_by = Column(String(128), nullable=True)
    details = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
