"""Location model for server physical location tracking"""
from sqlalchemy import Column, Integer, String, Text
from app.core.database import Base


class Location(Base):
    """Physical location where servers are housed"""
    __tablename__ = "locations"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    
    def __repr__(self):
        return f"<Location(id={self.id}, name='{self.name}')>"



