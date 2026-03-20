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
        model: Optional[str] = None,
        description: Optional[str] = None,
        serial_number: Optional[str] = None,
        is_os_disk: bool = False
    ) -> Disk:
        """Create a new disk"""
        disk = Disk(
            server_id=server_id,
            type=type,
            capacity_gb=capacity_gb,
            model=model,
            description=description,
            serial_number=serial_number,
            is_os_disk=is_os_disk
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
    
    @staticmethod
    def get_os_disk(db: Session, server_id: int) -> Optional[Disk]:
        """
        Get the OS disk for a server.
        
        Priority:
        1. Disk with is_os_disk=True
        2. Disk matching serial_number (if provided)
        3. First disk matching size and type (closest match)
        
        Returns:
            Disk object or None if no disks found
        """
        disks = DiskDAO.get_by_server(db, server_id)
        
        if not disks:
            return None
        
        # First priority: disk marked as OS disk
        os_disk = next((d for d in disks if d.is_os_disk), None)
        if os_disk:
            return os_disk
        
        # Second priority: match by serial number (if any disk has serial)
        # Note: We don't have the target serial here, so we'll skip this for now
        # The script will match by serial at runtime
        
        # Third priority: return first disk (closest size/type matching happens in script)
        return disks[0]
    
    @staticmethod
    def find_disk_by_serial(db: Session, server_id: int, serial_number: str) -> Optional[Disk]:
        """Find a disk by serial number for a server"""
        return db.query(Disk).filter(
            Disk.server_id == server_id,
            Disk.serial_number == serial_number
        ).first()



