"""Extension row for bare-metal (and http_proxy) services — links Service to Server."""

from sqlalchemy import Column, Integer, ForeignKey
from sqlalchemy.orm import relationship

from app.core.database import Base


class ServiceBareMetal(Base):
    __tablename__ = "service_bare_metal"

    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False, index=True)

    service = relationship("Service", back_populates="bare_metal")
    server = relationship("Server", backref="bare_metal_service_links")
