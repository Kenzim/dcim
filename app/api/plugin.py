from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.plugins.registry import get_registry
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_model=List[dict])
async def list_plugins(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all available server plugins with their metadata and capabilities (with UI schema)."""
    registry = get_registry()
    return registry.list_plugins()


@router.get("/{plugin_name}")
async def get_plugin_details(
    plugin_name: str,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get detailed information about a specific server plugin including capabilities with UI schema."""
    registry = get_registry()
    plugin_info = registry.get_plugin_info(plugin_name)
    if not plugin_info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{plugin_name}' not found"
        )
    return plugin_info


@router.post("/sync")
async def sync_plugins(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    No-op: Plugins are no longer stored in the database.
    This endpoint exists for backward compatibility but does nothing.
    """
    return {
        "message": "Plugins are no longer stored in the database. They are loaded directly from disk.",
        "results": {
            "created": [],
            "updated": [],
            "errors": []
        }
    }


class PluginTestRequest(BaseModel):
    """Request model for testing plugin capabilities"""
    plugin_config: Dict[str, Any]



