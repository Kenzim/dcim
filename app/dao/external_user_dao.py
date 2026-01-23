from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.external_user import ExternalUser


class ExternalUserDAO:
    """Data Access Object for ExternalUser model"""

    @staticmethod
    def create(
        db: Session,
        integration_id: int,
        external_user_id: str,
        external_username: Optional[str] = None,
        external_email: Optional[str] = None
    ) -> ExternalUser:
        """Create a new external user"""
        external_user = ExternalUser(
            integration_id=integration_id,
            external_user_id=external_user_id,
            external_username=external_username,
            external_email=external_email
        )
        db.add(external_user)
        db.commit()
        db.refresh(external_user)
        return external_user

    @staticmethod
    def get_by_id(db: Session, external_user_id: int) -> Optional[ExternalUser]:
        """Get external user by ID"""
        return db.query(ExternalUser).filter(ExternalUser.id == external_user_id).first()

    @staticmethod
    def get_by_external_id(db: Session, integration_id: int, external_user_id: str) -> Optional[ExternalUser]:
        """Get external user by integration ID and external user ID"""
        return db.query(ExternalUser).filter(
            ExternalUser.integration_id == integration_id,
            ExternalUser.external_user_id == external_user_id
        ).first()

    @staticmethod
    def get_by_integration(db: Session, integration_id: int) -> List[ExternalUser]:
        """Get all external users for a specific integration"""
        return db.query(ExternalUser).filter(ExternalUser.integration_id == integration_id).all()

    @staticmethod
    def update(db: Session, external_user: ExternalUser) -> ExternalUser:
        """Update an external user"""
        db.commit()
        db.refresh(external_user)
        return external_user

    @staticmethod
    def delete(db: Session, external_user_id: int) -> bool:
        """Delete an external user by ID"""
        external_user = db.query(ExternalUser).filter(ExternalUser.id == external_user_id).first()
        if external_user:
            db.delete(external_user)
            db.commit()
            return True
        return False
