"""Standalone proxy runner registration (not bound to bare-metal locations)."""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.core.database import Base


class ProxyRunner(Base):
    """A deployed proxy runner that phones home with a generated API key."""

    __tablename__ = "proxy_runners"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    api_key_encrypted = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    last_seen_at = Column(DateTime, nullable=True)
    last_seen_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<ProxyRunner(id={self.id}, name='{self.name}', enabled={self.enabled})>"
