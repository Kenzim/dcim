"""NFS storage for private networks: storage nodes, storage tenants and shares.

A storage tenant is the storage-side record of one ``nfs_storage`` service. It
is linked to exactly one private network and placed on one storage node,
whose storage runner pulls the desired state and reports back.
"""

import enum

from sqlalchemy import (
    BigInteger,
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


class VlanMode(str, enum.Enum):
    # Rackflow adds each customer VLAN to the node's switch ports.
    SWITCH = "switch"
    # The node already receives every VLAN (pre-trunked ports, VLAN-aware
    # bridge): no switch calls, attachments count as applied at once.
    PRECONFIGURED = "preconfigured"


class ShareState(str, enum.Enum):
    ACTIVE = "active"
    DELETING = "deleting"


class SftpAccess(str, enum.Enum):
    OFF = "off"
    READ_ONLY = "ro"
    READ_WRITE = "rw"


def _enum(enum_cls):
    return SQLEnum(enum_cls, native_enum=False, values_callable=lambda x: [e.value for e in x])


class StorageNode(Base):
    """A server running the storage runner, authenticated by its own API key."""

    __tablename__ = "storage_nodes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), nullable=False)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    # SHA-256 of the key; the plaintext is shown once on create and rotate.
    api_key_hash = Column(String(64), nullable=False, unique=True)
    api_key_prefix = Column(String(16), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    vlan_mode = Column(_enum(VlanMode), nullable=False, default=VlanMode.SWITCH)
    # Server port IDs that carry customer VLANs. Empty means the PXE uplink.
    vlan_port_ids = Column(JSON, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    last_seen_ip = Column(String(64), nullable=True)
    runner_version = Column(String(64), nullable=True)
    ceph_ok = Column(Boolean, nullable=True)
    free_bytes = Column(BigInteger, nullable=True)
    last_error = Column(Text, nullable=True)
    version_applied = Column(String(64), nullable=True)
    orphans = Column(JSON, nullable=True)
    # Public SFTP endpoint served by the node's runner.
    sftp_enabled = Column(Boolean, nullable=False, default=False)
    sftp_host = Column(String(255), nullable=True)
    sftp_port = Column(Integer, nullable=False, default=2222)
    # Reported by the runner.
    sftp_listening = Column(Boolean, nullable=True)
    sftp_host_key = Column(Text, nullable=True)
    sftp_host_key_fingerprint = Column(String(128), nullable=True)
    sftp_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    server = relationship("Server")


class StorageTenant(Base):
    """One storage service: its size, network link and placement.

    ``vlan``, ``network_cidr`` and ``endpoint_ip`` are copied from the network
    when the service is linked, so a terminated tenant keeps describing what
    the runner has to tear down after the network link is released.
    """

    __tablename__ = "storage_tenants"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="SET NULL"), nullable=True, unique=True, index=True)
    node_id = Column(Integer, ForeignKey("storage_nodes.id", ondelete="SET NULL"), nullable=True, index=True)
    network_id = Column(
        Integer, ForeignKey("private_networks.id", ondelete="SET NULL"), nullable=True, unique=True, index=True
    )
    location_id = Column(Integer, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True, index=True)
    vlan = Column(Integer, nullable=True)
    network_cidr = Column(String(32), nullable=True)
    endpoint_ip = Column(String(45), nullable=True)
    size_bytes = Column(BigInteger, nullable=False)
    # Reserved for per-tenant bandwidth/IOPS limits; nothing enforces it yet.
    limits = Column(JSON, nullable=True)
    terminated_at = Column(DateTime(timezone=True), nullable=True)
    destroy_after = Column(DateTime(timezone=True), nullable=True)
    runner_state = Column(String(16), nullable=True)
    runner_error = Column(Text, nullable=True)
    sftp_state = Column(String(16), nullable=True)
    sftp_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    service = relationship("Service")
    node = relationship("StorageNode")
    network = relationship("PrivateNetwork")
    location = relationship("Location")
    shares = relationship(
        "StorageShare",
        back_populates="tenant",
        cascade="all, delete-orphan",
        order_by="StorageShare.id",
    )
    sftp_users = relationship(
        "StorageSftpUser",
        back_populates="tenant",
        cascade="all, delete-orphan",
        order_by="StorageSftpUser.id",
    )


class StorageShare(Base):
    __tablename__ = "storage_shares"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_storage_share_name"),)

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("storage_tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(32), nullable=False)
    quota_bytes = Column(BigInteger, nullable=False)
    clients = Column(JSON, nullable=True)
    # Export to the whole private network instead of ``clients``.
    all_clients = Column(Boolean, nullable=False, default=True)
    read_only = Column(Boolean, nullable=False, default=False)
    sftp_access = Column(_enum(SftpAccess), nullable=False, default=SftpAccess.OFF)
    state = Column(_enum(ShareState), nullable=False, default=ShareState.ACTIVE, index=True)
    destroy_after = Column(DateTime(timezone=True), nullable=True)
    used_bytes = Column(BigInteger, nullable=True)
    runner_state = Column(String(16), nullable=True)
    runner_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    tenant = relationship("StorageTenant", back_populates="shares")


class StorageSftpUser(Base):
    """An SFTP login for one storage service.

    Only a bcrypt hash of the password is kept; the runner receives the hash
    and the public keys, never the password.
    """

    __tablename__ = "storage_sftp_users"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_storage_sftp_user_name"),)

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("storage_tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(32), nullable=False)
    # [{"key": "ssh-ed25519 AAAA... comment", "fingerprint": "SHA256:..."}]
    public_keys = Column(JSON, nullable=True)
    password_hash = Column(String(255), nullable=True)
    # CIDRs allowed to log in; empty means any address.
    allowed_ips = Column(JSON, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    last_login_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    tenant = relationship("StorageTenant", back_populates="sftp_users")
