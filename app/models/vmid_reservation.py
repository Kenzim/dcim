from sqlalchemy import Column, Integer, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class VMIDReservation(Base):
    __tablename__ = "vmid_reservations"
    __table_args__ = (
        UniqueConstraint("cluster_id", "vmid", name="uq_vmid_reservations_cluster_vmid"),
    )

    id = Column(Integer, primary_key=True, index=True)
    cluster_id = Column(Integer, ForeignKey("proxmox_clusters.id", ondelete="CASCADE"), nullable=False, index=True)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True)
    vmid = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    cluster = relationship("ProxmoxCluster", backref="vmid_reservations")
    service = relationship("Service", backref="vmid_reservation")
