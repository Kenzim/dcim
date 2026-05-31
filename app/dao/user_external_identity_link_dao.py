from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.user_external_identity_link import UserExternalIdentityLink


class UserExternalIdentityLinkDAO:
    @staticmethod
    def create(db: Session, user_id: int, external_user_id: int) -> UserExternalIdentityLink:
        row = UserExternalIdentityLink(user_id=user_id, external_user_id=external_user_id)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def get_by_id(db: Session, link_id: int) -> Optional[UserExternalIdentityLink]:
        return db.query(UserExternalIdentityLink).filter(UserExternalIdentityLink.id == link_id).first()

    @staticmethod
    def get_by_external_user_id(db: Session, external_user_id: int) -> Optional[UserExternalIdentityLink]:
        return (
            db.query(UserExternalIdentityLink)
            .filter(UserExternalIdentityLink.external_user_id == external_user_id)
            .first()
        )

    @staticmethod
    def list_for_user(db: Session, user_id: int) -> List[UserExternalIdentityLink]:
        return db.query(UserExternalIdentityLink).filter(UserExternalIdentityLink.user_id == user_id).all()

    @staticmethod
    def list_all(db: Session) -> List[UserExternalIdentityLink]:
        return db.query(UserExternalIdentityLink).all()

    @staticmethod
    def delete(db: Session, link_id: int) -> bool:
        row = db.query(UserExternalIdentityLink).filter(UserExternalIdentityLink.id == link_id).first()
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True
