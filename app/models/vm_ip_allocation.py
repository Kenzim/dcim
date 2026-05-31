from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    ForeignKey,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


vm_ip_allocation_cluster_association = Table(
    "vm_ip_allocation_clusters",
    Base.metadata,
    Column("vm_ip_allocation_id", Integer, ForeignKey("vm_ip_allocations.id", ondelete="CASCADE"), primary_key=True),
    Column("cluster_id", Integer, ForeignKey("proxmox_clusters.id", ondelete="CASCADE"), primary_key=True),
)


class VMIPAllocation(Base):
    __tablename__ = "vm_ip_allocations"
    __table_args__ = (UniqueConstraint("ip_address", name="uq_vm_ip_allocations_ip"),)

    id = Column(Integer, primary_key=True, index=True)
    ip_address = Column(String(64), nullable=False, unique=True, index=True)
    subnet_mask = Column(String(64), nullable=False)
    gateway = Column(String(64), nullable=False)
    bridge_name = Column(String(255), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    assigned_service_id = Column(
        Integer,
        ForeignKey("services.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    clusters = relationship(
        "ProxmoxCluster",
        secondary=vm_ip_allocation_cluster_association,
        backref="vm_ip_allocations",
    )
    # One VM service row points at one pool row (see ``service_vm.vm_ip_allocation_id``).
    service_vm_link = relationship(
        "ServiceVm",
        back_populates="vm_ip_allocation",
        uselist=False,
    )
    assigned_service = relationship(
        "Service",
        foreign_keys=[assigned_service_id],
        backref="assigned_vm_ip_allocations",
    )
