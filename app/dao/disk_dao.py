from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.disk import Disk, DiskType


class DiskDAO:
    """Data Access Object for Disk model"""

    @staticmethod
    def create(
        db: Session,
        server_id: int,
        type: DiskType,
        capacity_gb: int,
        description: Optional[str] = None
    ) -> Disk:
        """Create a new disk"""
        disk = Disk(
            server_id=server_id,
            type=type,
            capacity_gb=capacity_gb,
            description=description
        )
        db.add(disk)
        db.commit()
        db.refresh(disk)
        return disk

    @staticmethod
    def get_by_id(db: Session, disk_id: int) -> Optional[Disk]:
        """Get disk by ID"""
        return db.query(Disk).filter(Disk.id == disk_id).first()

    @staticmethod
    def get_by_server(db: Session, server_id: int) -> List[Disk]:
        """Get all disks for a server"""
        return db.query(Disk).filter(Disk.server_id == server_id).all()

    @staticmethod
    def update(db: Session, disk: Disk) -> Disk:
        """Update a disk"""
        db.commit()
        db.refresh(disk)
        return disk

    @staticmethod
    def delete(db: Session, disk_id: int) -> bool:
        """Delete a disk by ID"""
        disk = db.query(Disk).filter(Disk.id == disk_id).first()
        if disk:
            db.delete(disk)
            db.commit()
            return True
        return False

    @staticmethod
    def delete_by_server(db: Session, server_id: int) -> int:
        """Delete all disks for a server"""
        deleted = db.query(Disk).filter(Disk.server_id == server_id).delete()
        db.commit()
        return deleted



