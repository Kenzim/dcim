"""
Admin API endpoints for managing scripts.

These endpoints are for admin users to create, edit, and manage
scripts stored in the database.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao.script_dao import ScriptDAO
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/scripts", tags=["scripts-admin"])


class ScriptCreate(BaseModel):
    name: str
    content: str
    description: Optional[str] = None
    enabled: bool = True
    user_executable: bool = False


class ScriptUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    user_executable: Optional[bool] = None


class ScriptResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    content: str
    enabled: bool
    user_executable: bool
    created_at: str
    updated_at: str
    
    class Config:
        from_attributes = True


@router.post("", response_model=ScriptResponse, status_code=status.HTTP_201_CREATED)
async def create_script(
    script_data: ScriptCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Create a new script"""
    # Check if script with same name already exists
    existing = ScriptDAO.get_by_name(db, script_data.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Script with this name already exists"
        )
    
    script = ScriptDAO.create(
        db,
        name=script_data.name,
        content=script_data.content,
        description=script_data.description,
        enabled=script_data.enabled,
        user_executable=script_data.user_executable
    )
    
    logger.info(f"Created script '{script.name}' (ID: {script.id})")
    
    return ScriptResponse(
        id=script.id,
        name=script.name,
        description=script.description,
        content=script.content,
        enabled=script.enabled,
        user_executable=script.user_executable,
        created_at=script.created_at.isoformat(),
        updated_at=script.updated_at.isoformat()
    )


@router.get("", response_model=List[ScriptResponse])
async def list_scripts(
    enabled_only: bool = False,
    user_executable_only: bool = False,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all scripts"""
    logger.info(f"List scripts endpoint called by user {auth.get('username', 'unknown')}")
    scripts = ScriptDAO.get_all(db, enabled_only=enabled_only, user_executable_only=user_executable_only)
    logger.info(f"Found {len(scripts)} scripts")
    
    return [
        ScriptResponse(
            id=script.id,
            name=script.name,
            description=script.description,
            content=script.content,
            enabled=script.enabled,
            user_executable=script.user_executable,
            created_at=script.created_at.isoformat(),
            updated_at=script.updated_at.isoformat()
        )
        for script in scripts
    ]


@router.get("/{script_id}", response_model=ScriptResponse)
async def get_script(
    script_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get script details"""
    script = ScriptDAO.get_by_id(db, script_id)
    if not script:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Script not found"
        )
    
    return ScriptResponse(
        id=script.id,
        name=script.name,
        description=script.description,
        content=script.content,
        enabled=script.enabled,
        user_executable=script.user_executable,
        created_at=script.created_at.isoformat(),
        updated_at=script.updated_at.isoformat()
    )


@router.put("/{script_id}", response_model=ScriptResponse)
async def update_script(
    script_id: int,
    script_data: ScriptUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Update a script"""
    script = ScriptDAO.get_by_id(db, script_id)
    if not script:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Script not found"
        )
    
    # Check name uniqueness if changing name
    if script_data.name and script_data.name != script.name:
        existing = ScriptDAO.get_by_name(db, script_data.name)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Script with this name already exists"
            )
        script.name = script_data.name
    
    if script_data.content is not None:
        script.content = script_data.content
    if script_data.description is not None:
        script.description = script_data.description
    if script_data.enabled is not None:
        script.enabled = script_data.enabled
    if script_data.user_executable is not None:
        script.user_executable = script_data.user_executable
    
    ScriptDAO.update(db, script)
    db.refresh(script)
    
    return ScriptResponse(
        id=script.id,
        name=script.name,
        description=script.description,
        content=script.content,
        enabled=script.enabled,
        user_executable=script.user_executable,
        created_at=script.created_at.isoformat(),
        updated_at=script.updated_at.isoformat()
    )


@router.delete("/{script_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_script(
    script_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete a script"""
    success = ScriptDAO.delete(db, script_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Script not found"
        )
    
    logger.info(f"Deleted script (ID: {script_id})")
    return None
