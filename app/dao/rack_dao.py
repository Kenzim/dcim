from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.rack import Rack


class RackDAO:
    """Data Access Object for Rack model"""

    @staticmethod
    def create(
        db: Session,
        location_id: int,
        name: str,
        units: int = 42,
        description: Optional[str] = None,
        row: Optional[int] = None,
        row_position: Optional[int] = None
    ) -> Rack:
        """Create a new rack"""
        rack = Rack(
            location_id=location_id,
            name=name,
            units=units,
            description=description,
            row=row,
            row_position=row_position
        )
        db.add(rack)
        db.commit()
        db.refresh(rack)
        return rack

    @staticmethod
    def get_by_id(db: Session, rack_id: int) -> Optional[Rack]:
        """Get rack by ID"""
        return db.query(Rack).filter(Rack.id == rack_id).first()

    @staticmethod
    def get_by_location(db: Session, location_id: int) -> List[Rack]:
        """Get all racks in a location"""
        return db.query(Rack).filter(Rack.location_id == location_id).order_by(Rack.row, Rack.row_position, Rack.name).all()

    @staticmethod
    def get_by_row(db: Session, location_id: int, row: int) -> List[Rack]:
        """Get all racks in a specific row within a location"""
        return db.query(Rack).filter(
            Rack.location_id == location_id,
            Rack.row == row
        ).order_by(Rack.row_position).all()

    @staticmethod
    def get_all(db: Session) -> List[Rack]:
        """Get all racks"""
        return db.query(Rack).order_by(Rack.location_id, Rack.name).all()

    @staticmethod
    def get_by_name_and_location(db: Session, name: str, location_id: int) -> Optional[Rack]:
        """Get rack by name and location"""
        return db.query(Rack).filter(
            Rack.name == name,
            Rack.location_id == location_id
        ).first()

    @staticmethod
    def update(db: Session, rack: Rack) -> Rack:
        """Update a rack"""
        db.commit()
        db.refresh(rack)
        return rack

    @staticmethod
    def delete(db: Session, rack_id: int) -> bool:
        """Delete a rack by ID"""
        rack = db.query(Rack).filter(Rack.id == rack_id).first()
        if rack:
            db.delete(rack)
            db.commit()
            return True
        return False
