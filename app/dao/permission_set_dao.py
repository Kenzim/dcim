from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.permission_set import PermissionSet


class PermissionSetDAO:
    """Data Access Object for PermissionSet model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        permissions: Optional[dict] = None,
        description: Optional[str] = None,
        is_system: bool = False,
    ) -> PermissionSet:
        row = PermissionSet(
            name=name,
            description=description,
            permissions=permissions or {},
            is_system=is_system,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def get_by_id(db: Session, permission_set_id: int) -> Optional[PermissionSet]:
        return db.query(PermissionSet).filter(PermissionSet.id == permission_set_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[PermissionSet]:
        return db.query(PermissionSet).filter(PermissionSet.name == name).first()

    @staticmethod
    def get_all(db: Session) -> List[PermissionSet]:
        return db.query(PermissionSet).order_by(PermissionSet.name).all()

    @staticmethod
    def update(db: Session, row: PermissionSet, **kwargs) -> PermissionSet:
        for key, value in kwargs.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def delete(db: Session, permission_set_id: int) -> bool:
        row = db.query(PermissionSet).filter(PermissionSet.id == permission_set_id).first()
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True
