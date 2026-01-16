from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.location import Location


class LocationDAO:
    """Data Access Object for Location model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        description: Optional[str] = None
    ) -> Location:
        """Create a new location"""
        location = Location(
            name=name,
            description=description
        )
        db.add(location)
        db.commit()
        db.refresh(location)
        return location

    @staticmethod
    def get_by_id(db: Session, location_id: int) -> Optional[Location]:
        """Get location by ID"""
        return db.query(Location).filter(Location.id == location_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[Location]:
        """Get location by name"""
        return db.query(Location).filter(Location.name == name).first()

    @staticmethod
    def get_all(db: Session) -> List[Location]:
        """Get all locations"""
        return db.query(Location).order_by(Location.name).all()

    @staticmethod
    def update(db: Session, location: Location) -> Location:
        """Update a location"""
        db.commit()
        db.refresh(location)
        return location

    @staticmethod
    def delete(db: Session, location_id: int) -> bool:
        """Delete a location by ID"""
        location = db.query(Location).filter(Location.id == location_id).first()
        if location:
            db.delete(location)
            db.commit()
            return True
        return False



