"""Client private networks: one isolated layer 2 segment per network, per location."""

import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class SegmentationType(str, enum.Enum):
    """How a private network is carried. ``segment_id`` is the VLAN ID for ``vlan``."""

    VLAN = "vlan"


class PrivateNetworkStatus(str, enum.Enum):
    ACTIVE = "active"
    DELETING = "deleting"


class AttachmentState(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    ERROR = "error"
    DETACHING = "detaching"


def _enum(enum_cls):
    return SQLEnum(enum_cls, native_enum=False, values_callable=lambda x: [e.value for e in x])


class PrivateNetworkPool(Base):
    """Per-location settings: the segment ID range and the client limits."""

    __tablename__ = "private_network_pools"

    id = Column(Integer, primary_key=True, index=True)
    location_id = Column(
        Integer, ForeignKey("locations.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    enabled = Column(Boolean, nullable=False, default=True)
    vlan_start = Column(Integer, nullable=False)
    vlan_end = Column(Integer, nullable=False)
    # Last ID handed out. Allocation continues after it, so a released ID is
    # not reused until the range wraps.
    last_allocated = Column(Integer, nullable=True)
    # VLAN-aware Proxmox bridge that carries the range on every hypervisor.
    bridge = Column(String(64), nullable=False, default="vmbr0")
    max_networks_per_client = Column(Integer, nullable=False, default=5)
    max_networks_per_service = Column(Integer, nullable=False, default=4)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    location = relationship("Location")


class PrivateNetwork(Base):
    __tablename__ = "private_networks"
    __table_args__ = (
        UniqueConstraint("location_id", "segmentation_type", "segment_id", name="uq_private_network_segment"),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False, index=True)
    segmentation_type = Column(_enum(SegmentationType), nullable=False, default=SegmentationType.VLAN)
    segment_id = Column(Integer, nullable=False)
    cidr = Column(String(32), nullable=False)
    status = Column(_enum(PrivateNetworkStatus), nullable=False, default=PrivateNetworkStatus.ACTIVE, index=True)
    # False until the segment exists on every managed switch in the location.
    switch_synced = Column(Boolean, nullable=False, default=False)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    owner_user = relationship("User", foreign_keys=[owner_user_id])
    location = relationship("Location")
    attachments = relationship(
        "PrivateNetworkAttachment",
        back_populates="network",
        cascade="all, delete-orphan",
        order_by="PrivateNetworkAttachment.id",
    )


class PrivateNetworkAttachment(Base):
    """A service's membership of a private network.

    The row outlives the service link on purpose: it is only deleted once the
    VM NIC or the switch port has actually been cleaned up.
    """

    __tablename__ = "private_network_attachments"
    __table_args__ = (
        UniqueConstraint("network_id", "service_id", name="uq_private_network_attachment_service"),
        UniqueConstraint("network_id", "ip_address", name="uq_private_network_attachment_ip"),
    )

    id = Column(Integer, primary_key=True, index=True)
    network_id = Column(
        Integer, ForeignKey("private_networks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id = Column(Integer, ForeignKey("services.id", ondelete="SET NULL"), nullable=True, index=True)
    # Dedicated servers only: whose switch ports carry the segment.
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="SET NULL"), nullable=True, index=True)
    ip_address = Column(String(45), nullable=True)
    # VMs only: the Proxmox ``netN`` index and the NIC's MAC address.
    vm_net_index = Column(Integer, nullable=True)
    mac_address = Column(String(17), nullable=True)
    state = Column(_enum(AttachmentState), nullable=False, default=AttachmentState.PENDING, index=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    network = relationship("PrivateNetwork", back_populates="attachments")
    service = relationship("Service")
    server = relationship("Server")
