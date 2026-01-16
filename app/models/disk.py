"""Disk model for server storage"""
from sqlalchemy import Column, Integer, String, Text, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship
from app.core.database import Base
import enum


class DiskType(str, enum.Enum):
    """Disk type enumeration"""
    SSD = "ssd"
    HDD = "hdd"


class Disk(Base):
    """Storage disk attached to a server"""
    __tablename__ = "disks"
    
    id = Column(Integer, primary_key=True, index=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(SQLEnum(DiskType), nullable=False)
    capacity_gb = Column(Integer, nullable=False)  # Capacity in GB
    description = Column(Text, nullable=True)
    
    # Relationship
    server = relationship("Server", backref="disks")
    
    def __repr__(self):
        return f"<Disk(id={self.id}, type='{self.type.value}', capacity={self.capacity_gb}GB)>"



