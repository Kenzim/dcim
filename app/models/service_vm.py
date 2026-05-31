"""Extension row for VM services — Proxmox placement (no Server row)."""

import enum

from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Text, Enum as SQLEnum, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class VMGuestState(str, enum.Enum):
    UNPROVISIONED = "unprovisioned"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    STOPPED = "stopped"
    DESTROYED = "destroyed"
    ERROR = "error"


class ServiceVm(Base):
    __tablename__ = "service_vm"
    __table_args__ = (
        UniqueConstraint("proxmox_cluster_id", "proxmox_vmid", name="uq_service_vm_cluster_vmid"),
    )

    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), primary_key=True)
    proxmox_cluster_id = Column(
        Integer, ForeignKey("proxmox_clusters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    proxmox_node_name = Column(String(255), nullable=True)
    proxmox_vmid = Column(Integer, nullable=True)
    vm_template_id = Column(Integer, ForeignKey("vm_templates.id", ondelete="SET NULL"), nullable=True, index=True)
    vm_ip_allocation_id = Column(
        Integer,
        ForeignKey("vm_ip_allocations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    guest_state = Column(
        SQLEnum(VMGuestState, native_enum=False, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=VMGuestState.UNPROVISIONED,
        server_default=VMGuestState.UNPROVISIONED.value,
        index=True,
    )
    guest_last_error = Column(Text, nullable=True)
    guest_last_transition_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    service = relationship("Service", back_populates="vm")
    vm_template = relationship("VMTemplate", backref="service_vm_links")
    proxmox_cluster = relationship("ProxmoxCluster", backref="vm_service_extensions")
    vm_ip_allocation = relationship("VMIPAllocation", back_populates="service_vm_link")
