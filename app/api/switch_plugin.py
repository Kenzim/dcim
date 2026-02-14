from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.plugins.switch_registry import get_switch_registry
from app.core.plugin_capabilities import get_switch_plugin_capabilities
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_model=List[dict])
async def list_switch_plugins(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all available switch plugins with their metadata"""
    registry = get_switch_registry()
    plugins = registry.list_plugins()
    
    # Return plugins with hardcoded capabilities
    result = []
    for plugin in plugins:
        plugin_name = plugin["name"]
        capabilities = get_switch_plugin_capabilities(plugin_name)
        
        result.append({
            "name": plugin["name"],
            "version": plugin["version"],
            "supported_categories": capabilities,
            "config_template": plugin.get("config_template", {}),
            "available_capabilities": capabilities
        })
    
    return result


@router.get("/{plugin_name}")
async def get_switch_plugin_details(
    plugin_name: str,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get detailed information about a specific switch plugin"""
    registry = get_switch_registry()
    
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
                detail=f"Switch plugin '{plugin_name}' not found"
            )
        
        # Get capabilities from hardcoded mapping
        capabilities = get_switch_plugin_capabilities(plugin_name)
        
        return {
            **plugin_info,
            "supported_categories": capabilities,
            "available_capabilities": capabilities
        }
    except HTTPException:
        # Re-raise HTTP exceptions (like 404) as-is
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting switch plugin details for {plugin_name}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.post("/sync")
async def sync_switch_plugins(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    No-op: Switch plugins are no longer stored in the database.
    This endpoint exists for backward compatibility but does nothing.
    """
    return {
        "message": "Switch plugins are no longer stored in the database. They are loaded directly from disk.",
        "results": {
            "created": [],
            "updated": [],
            "errors": []
        }
    }
