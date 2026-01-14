from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from app.core.database import get_db
from app.core.auth import require_admin
from app.plugins.registry import get_registry
from app.dao import PluginDAO
from app.services.plugin_sync import sync_plugins_to_db

router = APIRouter()


@router.get("/", response_model=List[dict])
async def list_plugins(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all available plugins with their metadata"""
    registry = get_registry()
    plugins = registry.list_plugins()
    
    # Also get plugins from database to show which are registered
    db_plugins = PluginDAO.get_all(db)
    
    # Merge registry plugins with database status
    result = []
    for plugin in plugins:
        db_plugin = next((p for p in db_plugins if p.name == plugin["name"]), None)
        # Get categories from database if registered, otherwise from registry
        if db_plugin:
            categories = [cat.name for cat in db_plugin.categories]
        else:
            categories = plugin.get("supported_categories", [])
        
        result.append({
            "name": plugin["name"],
            "version": plugin["version"],
            "supported_categories": categories,
            "config_template": plugin.get("config_template", {}),
            "registered": db_plugin is not None,
            "db_id": db_plugin.id if db_plugin else None
        })
    
    return result


@router.get("/{plugin_name}")
async def get_plugin_details(
    plugin_name: str,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get detailed information about a specific plugin"""
    registry = get_registry()
    
    try:
        # Get plugin info from registry
        plugin_info = None
        for plugin in registry.list_plugins():
            if plugin["name"] == plugin_name:
                plugin_info = plugin
                break
        
        if not plugin_info:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Plugin '{plugin_name}' not found"
            )
        
        # Get database record if exists
        db_plugin = PluginDAO.get_by_name(db, plugin_name)
        
        return {
            **plugin_info,
            "registered": db_plugin is not None,
            "db_id": db_plugin.id if db_plugin else None,
            "db_description": db_plugin.description if db_plugin else None
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.post("/sync")
async def sync_plugins(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Sync plugins from Python files (registry) to the database.
    
    This endpoint discovers all plugins in app/plugins/ and creates/updates
    their records in the database, linking them to categories.
    """
    try:
        results = sync_plugins_to_db(db)
        return {
            "message": "Plugins synced successfully",
            "results": results
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error syncing plugins: {str(e)}"
        )

