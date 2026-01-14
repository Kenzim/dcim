from sqlalchemy import Column, Integer, ForeignKey, Table
from app.core.database import Base

# Junction table for many-to-many relationship between plugins and categories
plugin_categories = Table(
    'plugin_categories',
    Base.metadata,
    Column('plugin_id', Integer, ForeignKey('plugins.id'), primary_key=True),
    Column('category_id', Integer, ForeignKey('categories.id'), primary_key=True)
)

