from sqlalchemy import Column, Integer, String, JSON, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class Server(Base):
    """
    Server model - represents a physical or virtual server in the DCIM system.
    
    Each server is linked to a plugin that defines how to interface with it.
    The plugin_config stores plugin-specific configuration (credentials, IP, etc.).
    """
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, index=True, nullable=False)
    server_ip = Column(String(45), nullable=False)  # IPv4 or IPv6
    description = Column(Text, nullable=True)
    cpu_count = Column(Integer, default=1, nullable=False)
    cpu_model = Column(String(255), nullable=True)
    ram_gb = Column(Integer, nullable=True)  # RAM amount in GB
    port_speed_mbps = Column(Integer, nullable=True)  # Port speed in Mbps (e.g., 1000 for 1Gbps, 10000 for 10Gbps)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False, index=True)
    plugin_id = Column(Integer, ForeignKey("plugins.id"), nullable=False, index=True)
    plugin_config = Column(JSON, nullable=False)  # Plugin-specific configuration (IPMI IP, credentials, etc.)
    enabled = Column(Boolean, default=True, nullable=False)
    # Capabilities tracking (per-server, since capabilities may differ per server)
    tested_capabilities = Column(JSON, nullable=True)  # List of capabilities that were successfully tested for this server
    test_logs = Column(Text, nullable=True)  # Logs from the last capability test run for this server
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    plugin = relationship("Plugin", backref="servers")
    location = relationship("Location", backref="servers")

    def __repr__(self):
        return f"<Server(name='{self.name}', server_ip='{self.server_ip}', plugin_id={self.plugin_id})>"
