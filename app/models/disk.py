"""Disk model for server storage"""
from sqlalchemy import Column, Integer, String, Text, Boolean, ForeignKey, Enum as SQLEnum, TypeDecorator
from sqlalchemy.orm import relationship
from app.core.database import Base
import enum


class DiskType(str, enum.Enum):
    """Disk type enumeration"""
    SSD = "SSD"
    HDD = "HDD"
    
    @classmethod
    def _missing_(cls, value):
        """Handle case-insensitive enum values from database"""
        if isinstance(value, str):
            value_upper = value.upper()
            for member in cls:
                if member.value.upper() == value_upper:
                    return member
        return None


class CaseInsensitiveEnum(TypeDecorator):
    """SQLAlchemy type decorator that handles case-insensitive enum conversion"""
    impl = SQLEnum
    cache_ok = True
    
    def __init__(self, enum_class, *args, **kwargs):
        self.enum_class = enum_class
        super().__init__(enum_class, *args, **kwargs)
    
    def process_bind_param(self, value, dialect):
        """Convert enum to value for database"""
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value.value
        return str(value).upper()
    
    def process_result_value(self, value, dialect):
        """Convert database value to enum, handling case-insensitive matching"""
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value
        # Try exact match first
        try:
            return self.enum_class(value)
        except ValueError:
            # Try case-insensitive match
            value_upper = str(value).upper()
            for member in self.enum_class:
                if member.value.upper() == value_upper:
                    return member
            # If no match, raise error
            raise ValueError(f"Invalid enum value: {value}")


class Disk(Base):
    """Storage disk attached to a server"""
    __tablename__ = "disks"
    
    id = Column(Integer, primary_key=True, index=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(CaseInsensitiveEnum(DiskType), nullable=False)
    capacity_gb = Column(Integer, nullable=False)  # Capacity in GB
    model = Column(String(255), nullable=True)  # Hardware model (e.g., Samsung PM9A3)
    description = Column(Text, nullable=True)
    serial_number = Column(String(255), nullable=True, index=True)  # Disk serial number for matching
    is_os_disk = Column(Boolean, default=False, nullable=False, index=True)  # Flag to mark this as the OS installation disk
    
    # Relationship
    server = relationship("Server", backref="disks")
    
    def __repr__(self):
        return f"<Disk(id={self.id}, type='{self.type.value}', capacity={self.capacity_gb}GB, serial={self.serial_number}, is_os_disk={self.is_os_disk})>"



