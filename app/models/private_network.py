"""Client private networks: one isolated layer 2 segment per network, per location."""

import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    JSON,
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


class NetworkKind(str, enum.Enum):
    """``proxy_access`` networks use the fixed proxy access range and are joined by proxy runners."""

    STANDARD = "standard"
    PROXY_ACCESS = "proxy_access"


class PrivateNetworkStatus(str, enum.Enum):
    ACTIVE = "active"
    DELETING = "deleting"


class AttachmentState(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    ERROR = "error"
    DETACHING = "detaching"


class InvitationStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


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
    # Admin-only list of CIDRs that client networks may not overlap.
    reserved_cidrs = Column(JSON, nullable=True)
    # Proxy fast path: clients with a server and a proxy here may turn on a proxy access network.
    proxy_access_enabled = Column(Boolean, nullable=False, default=False)
    # Members (other clients) one network can have; 0 turns sharing off in this location.
    max_members_per_network = Column(Integer, nullable=False, default=5, server_default="5")
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
    kind = Column(_enum(NetworkKind), nullable=False, default=NetworkKind.STANDARD, index=True)
    status = Column(_enum(PrivateNetworkStatus), nullable=False, default=PrivateNetworkStatus.ACTIVE, index=True)
    # False until the segment exists on every managed switch in the location.
    switch_synced = Column(Boolean, nullable=False, default=False)
    last_error = Column(Text, nullable=True)
    # Shared networks only: when the owner was first seen with no live service.
    owner_gone_since = Column(DateTime(timezone=True), nullable=True)
    # DHCP served by the network's storage presence (docs/private-network-dhcp.md).
    dhcp_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    dhcp_range_start = Column(String(15), nullable=True)
    dhcp_range_end = Column(String(15), nullable=True)
    # Null means the PRIVATE_NETWORK_DHCP_LEASE_SECONDS default.
    dhcp_lease_seconds = Column(Integer, nullable=True)
    # {gateway, dns_servers, domain, mtu, routes: [{destination, gateway}], ntp_servers}
    dhcp_options = Column(JSON, nullable=True)
    # The storage node that reports serving the network presence, and since when.
    presence_node_id = Column(Integer, ForeignKey("storage_nodes.id", ondelete="SET NULL"), nullable=True)
    presence_since = Column(DateTime(timezone=True), nullable=True)
    dhcp_error = Column(String(2000), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    owner_user = relationship("User", foreign_keys=[owner_user_id])
    location = relationship("Location")
    presence_node = relationship("StorageNode", foreign_keys=[presence_node_id])
    dhcp_reservations = relationship(
        "PrivateNetworkDhcpReservation",
        back_populates="network",
        cascade="all, delete-orphan",
        order_by="PrivateNetworkDhcpReservation.id",
    )
    dhcp_leases = relationship(
        "PrivateNetworkDhcpLease",
        back_populates="network",
        cascade="all, delete-orphan",
        order_by="PrivateNetworkDhcpLease.id",
    )
    attachments = relationship(
        "PrivateNetworkAttachment",
        back_populates="network",
        cascade="all, delete-orphan",
        order_by="PrivateNetworkAttachment.id",
    )
    members = relationship(
        "PrivateNetworkMember",
        back_populates="network",
        cascade="all, delete-orphan",
        order_by="PrivateNetworkMember.id",
    )
    invitations = relationship(
        "PrivateNetworkInvitation",
        back_populates="network",
        cascade="all, delete-orphan",
        order_by="PrivateNetworkInvitation.id",
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
    # Proxy access networks only: the attachment is this runner, not a service.
    proxy_runner_id = Column(
        Integer, ForeignKey("proxy_runners.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    proxy_runner = relationship("ProxyRunner")


class PrivateNetworkMember(Base):
    """A client other than the owner who may attach their own services to a network."""

    __tablename__ = "private_network_members"
    __table_args__ = (UniqueConstraint("network_id", "user_id", name="uq_private_network_member"),)

    id = Column(Integer, primary_key=True, index=True)
    network_id = Column(
        Integer, ForeignKey("private_networks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    invited_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    joined_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    network = relationship("PrivateNetwork", back_populates="members")
    user = relationship("User", foreign_keys=[user_id])


class PrivateNetworkInvitation(Base):
    """An owner's invitation. ``invitee_user_id`` is empty when what the owner typed matched no eligible client."""

    __tablename__ = "private_network_invitations"
    __table_args__ = (UniqueConstraint("active_key", name="uq_private_network_invitation_active"),)

    id = Column(Integer, primary_key=True, index=True)
    network_id = Column(
        Integer, ForeignKey("private_networks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inviter_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    invitee_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    # What the owner typed (email or username), lower-cased. Shown to the owner only.
    invitee_label = Column(String(255), nullable=False)
    status = Column(_enum(InvitationStatus), nullable=False, default=InvitationStatus.PENDING, index=True)
    # Set only while pending: one pending invitation per network and invitee.
    active_key = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    responded_at = Column(DateTime(timezone=True), nullable=True)

    network = relationship("PrivateNetwork", back_populates="invitations")
    inviter = relationship("User", foreign_keys=[inviter_user_id])
    invitee = relationship("User", foreign_keys=[invitee_user_id])


class PrivateNetworkDhcpReservation(Base):
    """An owner's DHCP reservation: this MAC always gets this address on the network."""

    __tablename__ = "private_network_dhcp_reservations"
    __table_args__ = (
        UniqueConstraint("network_id", "mac", name="uq_private_network_dhcp_reservation_mac"),
        UniqueConstraint("network_id", "ip", name="uq_private_network_dhcp_reservation_ip"),
    )

    id = Column(Integer, primary_key=True, index=True)
    network_id = Column(
        Integer, ForeignKey("private_networks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Lower case, colon separated.
    mac = Column(String(17), nullable=False)
    ip = Column(String(15), nullable=False)
    label = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    network = relationship("PrivateNetwork", back_populates="dhcp_reservations")


class PrivateNetworkDhcpLease(Base):
    """A dynamic DHCP lease reported by the storage node serving the network."""

    __tablename__ = "private_network_dhcp_leases"
    __table_args__ = (UniqueConstraint("network_id", "mac", name="uq_private_network_dhcp_lease_mac"),)

    id = Column(Integer, primary_key=True, index=True)
    network_id = Column(
        Integer, ForeignKey("private_networks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mac = Column(String(17), nullable=False)
    ip = Column(String(15), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    hostname = Column(String(64), nullable=True)
    node_id = Column(Integer, ForeignKey("storage_nodes.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    network = relationship("PrivateNetwork", back_populates="dhcp_leases")
