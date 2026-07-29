from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.user import User


class UserDAO:
    """Data Access Object for User model"""

    @staticmethod
    def create(
        db: Session,
        username: str,
        email: str,
        password: Optional[str] = None,
        is_admin: bool = False,
        is_reseller: bool = False,
        reseller_id: Optional[int] = None,
        billing_integration_id: Optional[int] = None,
        external_user_id: Optional[str] = None,
        external_username: Optional[str] = None,
        external_email: Optional[str] = None,
    ) -> User:
        """Create a new user.

        ``password`` may be ``None``/blank for non-admin (client) accounts:
        the account still has full client portal access (impersonation,
        billing SSO), just no password login until one is set.

        ``billing_integration_id``/``external_user_id`` link this account to
        a billing identity (e.g. a WHMCS client) at creation time.
        """
        user = User(
            username=username,
            email=email,
            is_admin=is_admin,
            is_reseller=is_reseller,
            reseller_id=reseller_id,
            billing_integration_id=billing_integration_id,
            external_user_id=external_user_id,
            external_username=external_username,
            external_email=external_email,
        )
        user.set_password(password)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def get_by_id(db: Session, user_id: int) -> Optional[User]:
        """Get user by ID"""
        return db.query(User).filter(User.id == user_id).first()

    @staticmethod
    def get_by_username(db: Session, username: str) -> Optional[User]:
        """Get user by username"""
        return db.query(User).filter(User.username == username).first()

    @staticmethod
    def get_by_email(db: Session, email: str) -> Optional[User]:
        """Get user by email"""
        return db.query(User).filter(User.email == email).first()

    @staticmethod
    def get_by_billing_identity(db: Session, billing_integration_id: int, external_user_id: str) -> Optional[User]:
        """Get the user linked to a given (integration, external id) billing identity."""
        return (
            db.query(User)
            .filter(
                User.billing_integration_id == billing_integration_id,
                User.external_user_id == external_user_id,
            )
            .first()
        )

    @staticmethod
    def get_by_reseller_identity(
        db: Session, reseller_id: int, external_user_id: str
    ) -> Optional[User]:
        """Get a client by its identity inside one reseller tenant."""
        return (
            db.query(User)
            .filter(
                User.reseller_id == reseller_id,
                User.external_user_id == external_user_id,
            )
            .first()
        )

    @staticmethod
    def get_by_billing_integration(db: Session, billing_integration_id: int) -> List[User]:
        """All users with a billing identity under a given integration."""
        return db.query(User).filter(User.billing_integration_id == billing_integration_id).all()

    @staticmethod
    def get_all(db: Session, skip: int = 0, limit: int = 100) -> List[User]:
        """Get all users with pagination"""
        return db.query(User).offset(skip).limit(limit).all()

    @staticmethod
    def update(db: Session, user: User) -> User:
        """Update a user"""
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def delete(db: Session, user_id: int) -> bool:
        """Delete a user by ID"""
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            db.delete(user)
            db.commit()
            return True
        return False




