"""Rack model for organizing servers within locations"""
from sqlalchemy import Column, Integer, String, Text, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from app.core.database import Base


class Rack(Base):
    """Rack model - represents a physical rack within a location"""
    __tablename__ = "racks"
    
    id = Column(Integer, primary_key=True, index=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)
    units = Column(Integer, nullable=False, default=42)  # Number of rack units (U), default 42U
    units_start_from_bottom = Column(Boolean, nullable=False, default=True)  # True: 1U at bottom, False: 1U at top
    row = Column(Integer, nullable=True, index=True)  # Row number within the location
    row_position = Column(Integer, nullable=True)  # Position within the row (1, 2, 3, etc.)
    
    # Relationships
    location = relationship("Location", backref="racks")
    
    def __repr__(self):
        return f"<Rack(id={self.id}, name='{self.name}', location_id={self.location_id}, units={self.units})>"
