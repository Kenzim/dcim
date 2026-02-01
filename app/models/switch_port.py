"""Switch Port model for managing network switch ports"""
from sqlalchemy import Column, Integer, String, Text, ForeignKey, Boolean, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class SwitchPort(Base):
    """Network port on a switch"""
    __tablename__ = "switch_ports"
    
    id = Column(Integer, primary_key=True, index=True)
    switch_id = Column(Integer, ForeignKey("network_switches.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)  # Port name (e.g., "Port 1", "GigabitEthernet0/1", "eth0")
    if_index = Column(Integer, nullable=True)  # SNMP ifIndex (if available from plugin)
    speed_mbps = Column(Integer, nullable=True)  # Port speed in Mbps (e.g., 1000 for 1Gbps, 10000 for 10Gbps)
    admin_status = Column(Integer, nullable=True)  # Administrative status (1=up, 2=down, 3=testing)
    oper_status = Column(Integer, nullable=True)  # Operational status (1=up, 2=down, 3=testing, etc.)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    switch = relationship("NetworkSwitch", backref="switch_ports")
    
    def __repr__(self):
        status_info = f", admin={self.admin_status}, oper={self.oper_status}" if self.admin_status and self.oper_status else ""
        speed_info = f", {self.speed_mbps}Mbps" if self.speed_mbps else ""
        return f"<SwitchPort(id={self.id}, name='{self.name}', switch_id={self.switch_id}{speed_info}{status_info})>"
