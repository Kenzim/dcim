from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.plugin import ServerPlugin
from app.models.category import Category


class ServerPluginDAO:
    """Data Access Object for ServerPlugin model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        version: str,
        category_names: List[str],
        config_template: dict,
        description: Optional[str] = None
    ) -> ServerPlugin:
        """Create a new server plugin with categories"""
        plugin = ServerPlugin(
            name=name,
            version=version,
            config_template=config_template,
            description=description
        )
        
        # Add categories
        for category_name in category_names:
            category = db.query(Category).filter(Category.name == category_name).first()
            if category:
                plugin.categories.append(category)
        
        db.add(plugin)
        db.commit()
        db.refresh(plugin)
        return plugin

    @staticmethod
    def get_by_id(db: Session, plugin_id: int) -> Optional[ServerPlugin]:
        """Get server plugin by ID"""
        return db.query(ServerPlugin).filter(ServerPlugin.id == plugin_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[ServerPlugin]:
        """Get server plugin by name"""
        return db.query(ServerPlugin).filter(ServerPlugin.name == name).first()

    @staticmethod
    def get_all(db: Session, skip: int = 0, limit: int = 100) -> List[ServerPlugin]:
        """Get all server plugins with pagination"""
        return db.query(ServerPlugin).offset(skip).limit(limit).all()

    @staticmethod
    def update(db: Session, plugin: ServerPlugin) -> ServerPlugin:
        """Update a plugin"""
        db.commit()
        db.refresh(plugin)
        return plugin

    @staticmethod
    def delete(db: Session, plugin_id: int) -> bool:
        """Delete a server plugin by ID"""
        plugin = db.query(ServerPlugin).filter(ServerPlugin.id == plugin_id).first()
        if plugin:
            db.delete(plugin)
            db.commit()
            return True
        return False

