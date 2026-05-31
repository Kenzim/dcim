from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Boolean, JSON, ForeignKey, Float
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class ProxmoxCluster(Base):
    __tablename__ = "proxmox_clusters"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    api_url = Column(String(512), nullable=False)
    username = Column(String(255), nullable=False)
    password = Column(String(255), nullable=False)
    verify_ssl = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=True)
    vmid_min = Column(Integer, nullable=True)
    vmid_max = Column(Integer, nullable=True)
    details = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    nodes = relationship("ProxmoxNode", back_populates="cluster", cascade="all, delete-orphan")


class ProxmoxNode(Base):
    __tablename__ = "proxmox_nodes"

    id = Column(Integer, primary_key=True, index=True)
    cluster_id = Column(Integer, ForeignKey("proxmox_clusters.id", ondelete="CASCADE"), nullable=False, index=True)
    node_name = Column(String(255), nullable=False, index=True)
    enabled = Column(Boolean, nullable=False, default=True)
    details = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    cluster = relationship("ProxmoxCluster", back_populates="nodes")
    storages = relationship("ProxmoxStorage", back_populates="node", cascade="all, delete-orphan")
    templates = relationship("ProxmoxTemplate", back_populates="node", cascade="all, delete-orphan")
    capacity_snapshots = relationship("ProxmoxCapacitySnapshot", back_populates="node", cascade="all, delete-orphan")


class ProxmoxStorage(Base):
    __tablename__ = "proxmox_storages"

    id = Column(Integer, primary_key=True, index=True)
    node_id = Column(Integer, ForeignKey("proxmox_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    storage_name = Column(String(255), nullable=False, index=True)
    storage_type = Column(String(64), nullable=True)
    total_bytes = Column(BigInteger, nullable=True)
    used_bytes = Column(BigInteger, nullable=True)
    details = Column(JSON, nullable=False, default=dict)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    node = relationship("ProxmoxNode", back_populates="storages")


class ProxmoxTemplate(Base):
    __tablename__ = "proxmox_templates"

    id = Column(Integer, primary_key=True, index=True)
    node_id = Column(Integer, ForeignKey("proxmox_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    vmid = Column(Integer, nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    storage_name = Column(String(255), nullable=True)
    details = Column(JSON, nullable=False, default=dict)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    node = relationship("ProxmoxNode", back_populates="templates")


class ProxmoxCapacitySnapshot(Base):
    __tablename__ = "proxmox_capacity_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    node_id = Column(Integer, ForeignKey("proxmox_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    cpu_total = Column(Float, nullable=True)
    cpu_used = Column(Float, nullable=True)
    ram_total_bytes = Column(BigInteger, nullable=True)
    ram_used_bytes = Column(BigInteger, nullable=True)
    storage_total_bytes = Column(BigInteger, nullable=True)
    storage_used_bytes = Column(BigInteger, nullable=True)
    overcommit_ratio = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    node = relationship("ProxmoxNode", back_populates="capacity_snapshots")
