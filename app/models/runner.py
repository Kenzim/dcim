"""Unified location runner (DHCP / TFTP / media) that phones home over WebSocket."""
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class Runner(Base):
    """A remote runner enrolled with a generated API key that dials Rackflow."""

    __tablename__ = "runners"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    location_id = Column(Integer, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True, index=True)
    capabilities = Column(JSON, nullable=False)
    api_key_encrypted = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    last_seen_at = Column(DateTime, nullable=True)
    last_seen_ip = Column(String(64), nullable=True)
    agent_version = Column(String(64), nullable=True)
    state = Column(JSON, nullable=True)
    state_updated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    location = relationship("Location", backref="runners")

    def __repr__(self):
        return f"<Runner(id={self.id}, name={self.name!r}, capabilities={self.capabilities})>"
