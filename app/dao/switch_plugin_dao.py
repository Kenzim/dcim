from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.switch_plugin import SwitchPlugin


class SwitchPluginDAO:
    """Data Access Object for SwitchPlugin model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        version: str,
        config_template: dict,
        description: Optional[str] = None,
        available_capabilities: Optional[List[str]] = None
    ) -> SwitchPlugin:
        """Create a new switch plugin"""
        plugin = SwitchPlugin(
            name=name,
            version=version,
            description=description,
            config_template=config_template,
            available_capabilities=available_capabilities
        )
        db.add(plugin)
        db.commit()
        db.refresh(plugin)
        return plugin

    @staticmethod
    def get_by_id(db: Session, plugin_id: int) -> Optional[SwitchPlugin]:
        """Get plugin by ID"""
        return db.query(SwitchPlugin).filter(SwitchPlugin.id == plugin_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[SwitchPlugin]:
        """Get plugin by name"""
        return db.query(SwitchPlugin).filter(SwitchPlugin.name == name).first()

    @staticmethod
    def get_all(db: Session) -> List[SwitchPlugin]:
        """Get all switch plugins"""
        return db.query(SwitchPlugin).order_by(SwitchPlugin.name).all()

    @staticmethod
    def update(db: Session, plugin: SwitchPlugin) -> SwitchPlugin:
        """Update a plugin"""
        db.commit()
        db.refresh(plugin)
        return plugin

    @staticmethod
    def delete(db: Session, plugin_id: int) -> bool:
        """Delete a plugin by ID"""
        plugin = db.query(SwitchPlugin).filter(SwitchPlugin.id == plugin_id).first()
        if plugin:
            db.delete(plugin)
            db.commit()
            return True
        return False

    @staticmethod
    def get_or_create(
        db: Session,
        name: str,
        version: str,
        config_template: dict,
        description: Optional[str] = None,
        available_capabilities: Optional[List[str]] = None
    ) -> SwitchPlugin:
        """Get existing plugin or create if it doesn't exist"""
        plugin = SwitchPluginDAO.get_by_name(db, name)
        if plugin:
            return plugin
        return SwitchPluginDAO.create(db, name, version, config_template, description, available_capabilities)
