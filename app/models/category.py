from sqlalchemy import Column, Integer, String, Text
from sqlalchemy.orm import relationship
from app.core.database import Base


class Category(Base):
    """
    Category model - represents a category of functionality that plugins can support.
    
    Categories: power_control, user_account_control, boot_order_control
    """
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, index=True, nullable=False)  # e.g., "power_control"
    display_name = Column(String(255), nullable=False)  # e.g., "Power Control"
    description = Column(Text, nullable=True)

    # Many-to-many relationship with plugins
    plugins = relationship("Plugin", secondary="plugin_categories", back_populates="categories")

    def __repr__(self):
        return f"<Category(name='{self.name}')>"

