from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.category import Category


class CategoryDAO:
    """Data Access Object for Category model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        display_name: str,
        description: Optional[str] = None
    ) -> Category:
        """Create a new category"""
        category = Category(
            name=name,
            display_name=display_name,
            description=description
        )
        db.add(category)
        db.commit()
        db.refresh(category)
        return category

    @staticmethod
    def get_by_id(db: Session, category_id: int) -> Optional[Category]:
        """Get category by ID"""
        return db.query(Category).filter(Category.id == category_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[Category]:
        """Get category by name"""
        return db.query(Category).filter(Category.name == name).first()

    @staticmethod
    def get_all(db: Session) -> List[Category]:
        """Get all categories"""
        return db.query(Category).all()

    @staticmethod
    def get_or_create(db: Session, name: str, display_name: str, description: Optional[str] = None) -> Category:
        """Get category by name, or create if it doesn't exist"""
        category = CategoryDAO.get_by_name(db, name)
        if not category:
            category = CategoryDAO.create(db, name, display_name, description)
        return category



