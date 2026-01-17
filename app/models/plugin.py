from sqlalchemy import Column, Integer, String, JSON, DateTime, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class Plugin(Base):
    """
    Plugin model - stores metadata about available server management plugins.
    
    Each plugin represents a different method of interfacing with servers
    (e.g., IPMI, Proxmox, SSH, vendor APIs).
    """
    __tablename__ = "plugins"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, index=True, nullable=False)
    version = Column(String(50), nullable=False)
    description = Column(Text, nullable=True)
    config_template = Column(JSON, nullable=False)  # JSON schema for plugin configuration
    # Capabilities tracking (plugin-level - what the plugin claims to support)
    available_capabilities = Column(JSON, nullable=True)  # List of capabilities the plugin claims to support
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Many-to-many relationship with categories
    categories = relationship("Category", secondary="plugin_categories", back_populates="plugins")

    def __repr__(self):
        return f"<Plugin(name='{self.name}', version='{self.version}')>"
