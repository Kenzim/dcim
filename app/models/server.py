from sqlalchemy import Column, Integer, String, JSON, Boolean, DateTime, ForeignKey, Text, Enum as SQLEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base
import enum


class BootMode(str, enum.Enum):
    """Boot mode options"""
    BIOS = "bios"
    UEFI = "uefi"


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
    rack_id = Column(Integer, ForeignKey("racks.id"), nullable=True, index=True)  # Optional rack assignment
    rack_unit = Column(Integer, nullable=True)  # Rack unit position (1-based, e.g., 1-42 for a 42U rack)
    plugin_id = Column(Integer, ForeignKey("plugins.id"), nullable=False, index=True)
    plugin_config = Column(JSON, nullable=False)  # Plugin-specific configuration (IPMI IP, credentials, etc.)
    enabled = Column(Boolean, default=True, nullable=False)
    boot_mode = Column(SQLEnum(BootMode, native_enum=False, values_callable=lambda x: [e.value for e in x]), nullable=False, default=BootMode.UEFI)  # Boot mode: UEFI or BIOS (deprecated - use pxe_boot_mode and os_boot_mode)
    pxe_boot_mode = Column(SQLEnum(BootMode, native_enum=False, values_callable=lambda x: [e.value for e in x]), nullable=False, default=BootMode.UEFI)  # PXE boot mode: controls what DHCP serves initially (UEFI or BIOS)
    os_boot_mode = Column(SQLEnum(BootMode, native_enum=False, values_callable=lambda x: [e.value for e in x]), nullable=False, default=BootMode.UEFI)  # OS boot mode: controls how the server boots the installed OS (UEFI or BIOS)
    # Capabilities tracking (per-server, since capabilities may differ per server)
    tested_capabilities = Column(JSON, nullable=True)  # List of capabilities that were successfully tested for this server
    test_logs = Column(Text, nullable=True)  # Logs from the last capability test run for this server
    # Credentials storage (OS passwords, etc.)
    credentials = Column(JSON, nullable=True)  # Store OS installation passwords and other credentials (e.g., {"admin_password": "...", "os_type": "windows"})
    external_user_id = Column(Integer, ForeignKey("external_users.id"), nullable=True, index=True)  # Link to external user (if provisioned via billing system)
    # IPMI Web Proxy configuration
    ipmi_proxy_enabled = Column(Boolean, default=False, nullable=False)  # Enable/disable IPMI web proxy
    ipmi_web_management_url = Column(String(512), nullable=True)  # URL for IPMI web management interface
    ipmi_viewer_username = Column(String(255), nullable=True)  # Username for IPMI web access (read-only)
    ipmi_viewer_password = Column(String(255), nullable=True)  # Password for IPMI web access (read-only)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    plugin = relationship("Plugin", backref="servers")
    location = relationship("Location", backref="servers")
    rack = relationship("Rack", backref="servers")
    external_user = relationship("ExternalUser", back_populates="servers")

    def __repr__(self):
        return f"<Server(name='{self.name}', server_ip='{self.server_ip}', plugin_id={self.plugin_id})>"
