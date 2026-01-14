from sqlalchemy import Column, Integer, String, JSON, Boolean, DateTime, ForeignKey
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
    hostname = Column(String(255), unique=True, index=True, nullable=False)
    display_name = Column(String(255), nullable=True)  # Optional friendly name
    plugin_id = Column(Integer, ForeignKey("plugins.id"), nullable=False, index=True)
    plugin_config = Column(JSON, nullable=False)  # Plugin-specific configuration
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationship
    plugin = relationship("Plugin", backref="servers")

    def __repr__(self):
        return f"<Server(hostname='{self.hostname}', plugin_id={self.plugin_id})>"
