"""DHCP server configuration stored in the database."""
from sqlalchemy import Column, Integer, String, Boolean, Text, JSON, ForeignKey
from app.core.database import Base


class DHCPConfigModel(Base):
    __tablename__ = "dhcp_config"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    service_instance_id = Column(Integer, ForeignKey("service_instances.id", ondelete="CASCADE"), nullable=True, index=True, unique=True)
    enabled = Column(Boolean, default=True, nullable=False)
    interfaces = Column(JSON, nullable=False)  # list of {interface, ip, cidr?, netmask?, gateway?}
    dns_servers = Column(JSON, nullable=True)  # list of strings or null
    hand_out_leases = Column(Boolean, default=True, nullable=False)
    default_lease_time = Column(Integer, default=3600, nullable=False)
    max_lease_time = Column(Integer, default=7200, nullable=False)
    config_file_path = Column(String(512), nullable=False)
    lease_file_path = Column(String(512), nullable=False)
