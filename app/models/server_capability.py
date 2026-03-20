from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class ServerCapability(Base):
    """Per-server capability enablement/override state."""

    __tablename__ = "server_capabilities"

    id = Column(Integer, primary_key=True, index=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    capability_id = Column(String(100), nullable=False, index=True)
    enabled = Column(Boolean, nullable=False, default=False)
    support_status = Column(String(32), nullable=True)  # unknown|supported|unsupported
    source = Column(String(32), nullable=True)  # default|manual|probe
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("server_id", "capability_id", name="uq_server_capability_server_capability"),
    )

    server = relationship("Server", backref="server_capabilities")

    def __repr__(self):
        return f"<ServerCapability(server_id={self.server_id}, capability_id='{self.capability_id}', enabled={self.enabled})>"
