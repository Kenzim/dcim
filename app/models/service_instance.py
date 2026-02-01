"""Service instance model - per-location DHCP/TFTP runner registration."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.core.database import Base


class ServiceInstance(Base):
    """A deployed DHCP or TFTP runner at a location, registered with base_url + API key."""

    __tablename__ = "service_instances"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    location_id = Column(Integer, ForeignKey("locations.id", ondelete="CASCADE"), nullable=False, index=True)
    service_type = Column(String(32), nullable=False, index=True)  # 'dhcp' | 'tftp'
    name = Column(String(255), nullable=False)
    base_url = Column(String(512), nullable=False)
    api_key_encrypted = Column(Text, nullable=False)
    last_connection_test = Column(DateTime, nullable=True)
    connection_ok = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    location = relationship("Location", backref="service_instances")

    def __repr__(self):
        return f"<ServiceInstance(id={self.id}, name='{self.name}', type='{self.service_type}', location_id={self.location_id})>"
