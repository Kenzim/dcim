"""Standalone proxy runner registration (not bound to bare-metal locations)."""
from datetime import datetime

import enum

from sqlalchemy import Boolean, Column, DateTime, Enum as SQLEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class ProxyRunnerVlanMode(str, enum.Enum):
    """How a runner receives the VLANs of proxy access networks.

    ``preconfigured``: the operator carries the pool's VLAN range to the runner
    once and Rackflow makes no switch calls. ``switch``: Rackflow trunks each
    VLAN on the uplink of the runner's server, like a dedicated server.
    """

    PRECONFIGURED = "preconfigured"
    SWITCH = "switch"


class ProxyRunner(Base):
    """A deployed proxy runner that phones home with a generated API key."""

    __tablename__ = "proxy_runners"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    api_key_encrypted = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    last_seen_at = Column(DateTime, nullable=True)
    last_seen_ip = Column(String(64), nullable=True)
    # Proxy fast path: where the runner is and how it joins access networks.
    # A runner without a location takes no part in the fast path.
    location_id = Column(Integer, ForeignKey("locations.id", ondelete="SET NULL"), nullable=True, index=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="SET NULL"), nullable=True, index=True)
    vlan_mode = Column(
        SQLEnum(ProxyRunnerVlanMode, native_enum=False, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=ProxyRunnerVlanMode.PRECONFIGURED,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    location = relationship("Location")
    server = relationship("Server")

    def __repr__(self):
        return f"<ProxyRunner(id={self.id}, name='{self.name}', enabled={self.enabled})>"
