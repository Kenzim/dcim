"""Network Switch model for managing network switches"""
from sqlalchemy import Column, Integer, String, JSON, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class NetworkSwitch(Base):
    """
    Network Switch model - represents a network switch in the Rackflow system.
    
    Each switch is linked to a plugin that defines how to interface with it.
    The plugin_config stores plugin-specific configuration (SNMP community, credentials, etc.).
    """
    __tablename__ = "network_switches"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, index=True, nullable=False)
    description = Column(Text, nullable=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False, index=True)
    rack_id = Column(Integer, ForeignKey("racks.id"), nullable=True, index=True)  # Optional rack assignment
    rack_unit = Column(Integer, nullable=True)  # Bottom (lowest) U the switch occupies (1-based)
    rack_units = Column(Integer, nullable=False, default=1)  # Height in U. Switch occupies U [rack_unit, rack_unit+rack_units-1]
    plugin_name = Column(String(255), nullable=False, index=True)  # Plugin name (folder name on disk)
    plugin_config = Column(JSON, nullable=False)  # Plugin-specific configuration (SNMP community, credentials, etc.)
    enabled = Column(Boolean, default=True, nullable=False)
    port_count = Column(Integer, nullable=True)  # Number of ports on the switch
    model = Column(String(255), nullable=True)  # Switch model/manufacturer
    serial_number = Column(String(255), nullable=True)  # Switch serial number
    firmware_version = Column(String(255), nullable=True)  # Firmware/OS version
    # Capabilities tracking (per-switch, since capabilities may differ per switch)
    tested_capabilities = Column(JSON, nullable=True)  # List of capabilities that were successfully tested for this switch
    test_logs = Column(Text, nullable=True)  # Logs from the last capability test run for this switch
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    location = relationship("Location", backref="network_switches")
    rack = relationship("Rack", backref="network_switches")
    
    def __repr__(self):
        return f"<NetworkSwitch(name='{self.name}', plugin_name='{self.plugin_name}')>"
