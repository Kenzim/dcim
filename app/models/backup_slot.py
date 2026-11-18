"""Backup slots: purchasable places to keep one backup beyond the normal backup quota.

A ``BackupSlot`` is the backup-side record of one ``backup_slot`` service. It
holds at most one ``RetainedBackup``, whose archive stays on the Proxmox backup
storage under Proxmox's ``protected`` flag so pruning and VM termination never
remove it. A retained backup belongs to the client (``owner_user_id``), not to
the VM it was taken from, so it can be restored onto any of the client's VMs.
"""

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base

RESOURCE_VM = "vm"


class BackupSlot(Base):
    __tablename__ = "backup_slots"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    # Largest backup the slot accepts.
    size_bytes = Column(BigInteger, nullable=False)
    # Set on termination: the retained backup is deleted once destroy_after has passed.
    terminated_at = Column(DateTime(timezone=True), nullable=True)
    destroy_after = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    service = relationship("Service", foreign_keys=[service_id])
    retained = relationship(
        "RetainedBackup",
        back_populates="slot",
        uselist=False,
        cascade="all, delete-orphan",
    )


class RetainedBackup(Base):
    __tablename__ = "retained_backups"

    id = Column(Integer, primary_key=True, index=True)
    slot_id = Column(Integer, ForeignKey("backup_slots.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    # What kind of machine was backed up; only "vm" exists today.
    resource_type = Column(String(32), nullable=False, default=RESOURCE_VM, server_default=RESOURCE_VM)
    source_service_id = Column(Integer, ForeignKey("services.id", ondelete="SET NULL"), nullable=True, index=True)
    source_service_name = Column(String(255), nullable=True)
    proxmox_cluster_id = Column(Integer, ForeignKey("proxmox_clusters.id", ondelete="SET NULL"), nullable=True)
    proxmox_node_name = Column(String(128), nullable=True)
    source_vmid = Column(Integer, nullable=True)
    storage = Column(String(128), nullable=False)
    volid = Column(String(512), nullable=False, unique=True)
    name = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    # Size of the backed-up primary disk; a restore target's disk may not be smaller.
    disk_bytes = Column(BigInteger, nullable=True)
    vm_template_id = Column(Integer, nullable=True)
    template_code = Column(String(128), nullable=True)
    os_type = Column(String(64), nullable=True)
    rf_token = Column(String(255), nullable=True)
    backup_created_at = Column(DateTime(timezone=True), nullable=True)
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    slot = relationship("BackupSlot", back_populates="retained")
    source_service = relationship("Service", foreign_keys=[source_service_id])
