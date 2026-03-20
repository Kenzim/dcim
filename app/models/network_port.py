"""Network port model for server network interfaces"""
from sqlalchemy import Column, Integer, String, Text, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from app.core.database import Base


class NetworkPort(Base):
    """Network port/interface attached to a server"""
    __tablename__ = "network_ports"
    
    id = Column(Integer, primary_key=True, index=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)  # Port name (e.g., "eth0", "enp1s0", "Port 1")
    mac_address = Column(String(17), nullable=True)  # MAC address (e.g., "00:0e:1e:6f:16:b0")
    speed_mbps = Column(Integer, nullable=False)  # Port speed in Mbps (e.g., 1000 for 1Gbps, 10000 for 10Gbps)
    model = Column(String(255), nullable=True)  # NIC model/chipset
    pci_address = Column(String(32), nullable=True)  # PCI address for physical adapter matching
    is_physical = Column(Boolean, default=True, nullable=False)  # True for physical PCIe adapters
    lag_group = Column(String(255), nullable=True)  # LAG/bonding group name (e.g., "bond0", "lag1") - ports with same group are bonded
    monitor_bandwidth = Column(Boolean, default=False, nullable=False)  # Whether to monitor bandwidth for this port
    pxe_boot = Column(Boolean, default=False, nullable=False)  # Whether this port is used for PXE boot (max 1 per server)
    pxe_ip = Column(String(45), nullable=True)  # IP address for PXE boot (IPv4 or IPv6)
    description = Column(Text, nullable=True)
    
    # Relationship
    server = relationship("Server", backref="network_ports")
    
    def __repr__(self):
        lag_info = f", lag={self.lag_group}" if self.lag_group else ""
        monitor_info = ", monitored" if self.monitor_bandwidth else ""
        mac_info = f", mac={self.mac_address}" if self.mac_address else ""
        pxe_info = f", pxe={self.pxe_ip}" if self.pxe_boot else ""
        return f"<NetworkPort(id={self.id}, name='{self.name}', speed={self.speed_mbps}Mbps{mac_info}{lag_info}{monitor_info}{pxe_info})>"
