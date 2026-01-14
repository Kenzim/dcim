"""
Service to sync plugins from the registry (Python files) into the database.
"""
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from app.plugins.registry import get_registry
from app.dao import PluginDAO, CategoryDAO
from app.models.plugin import Plugin
from app.models.category import Category
import logging

logger = logging.getLogger(__name__)


def sync_plugins_to_db(db: Session) -> Dict[str, Any]:
    """
    Sync plugins from the registry to the database.
    
    This function:
    1. Discovers all plugins from the registry
    2. Creates/updates plugin records in the database
    3. Links plugins to their supported categories
    
    Returns:
        Dictionary with sync results (created, updated, errors)
    """
    registry = get_registry()
    registry_plugins = registry.list_plugins()
    
    results = {
        "created": [],
        "updated": [],
        "errors": []
    }
    
    # Get all existing plugins from database
    db_plugins = PluginDAO.get_all(db)
    db_plugins_by_name = {p.name: p for p in db_plugins}
    
    # Get all categories from database
    db_categories = CategoryDAO.get_all(db)
    categories_by_name = {cat.name: cat for cat in db_categories}
    
    for plugin_info in registry_plugins:
        plugin_name = plugin_info["name"]
        
        try:
            # Get or create category records for this plugin's supported categories
            plugin_categories = []
            for category_name in plugin_info.get("supported_categories", []):
                # Category should already exist from seed, but create if missing
                category = CategoryDAO.get_or_create(
                    db,
                    name=category_name,
                    display_name=category_name.replace("_", " ").title(),
                    description=f"Category: {category_name}"
                )
                plugin_categories.append(category)
            
            # Check if plugin exists in database
            db_plugin = db_plugins_by_name.get(plugin_name)
            
            if db_plugin:
                # Update existing plugin
                db_plugin.version = plugin_info["version"]
                db_plugin.config_template = plugin_info.get("config_template", {})
                
                # Update categories relationship
                # Clear existing categories and add new ones
                db_plugin.categories.clear()
                db_plugin.categories.extend(plugin_categories)
                
                PluginDAO.update(db, db_plugin)
                results["updated"].append(plugin_name)
                logger.info(f"Updated plugin: {plugin_name}")
            else:
                # Create new plugin
                new_plugin = PluginDAO.create(
                    db,
                    name=plugin_name,
                    version=plugin_info["version"],
                    category_names=[cat.name for cat in plugin_categories],
                    config_template=plugin_info.get("config_template", {}),
                    description=None
                )
                results["created"].append(plugin_name)
                logger.info(f"Created plugin: {plugin_name}")
        
        except Exception as e:
            error_msg = f"Error syncing plugin {plugin_name}: {str(e)}"
            results["errors"].append(error_msg)
            logger.error(error_msg, exc_info=True)
    
    return results

