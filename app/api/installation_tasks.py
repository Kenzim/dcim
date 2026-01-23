"""
API endpoints for installation task management
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.services.download_token_service import get_download_token_service
from app.dao.installation_task_dao import InstallationTaskDAO
from app.models.installation_task import InstallationTask, InstallationStatus
from app.dao.server_dao import ServerDAO
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/servers/{server_id}/installation-tasks", tags=["installation-tasks"])


class InstallationTaskLogUpdate(BaseModel):
    """Request model for updating installation task logs"""
    logs: str
    status: Optional[str] = None  # "completed" or "failed"
    error_message: Optional[str] = None


class InstallationTaskResponse(BaseModel):
    """Response model for installation task"""
    id: int
    server_id: int
    boot_task_id: int
    template_id: Optional[str] = None
    template_parameters: Optional[dict] = None
    status: str
    os_name: Optional[str] = None
    os_version: Optional[str] = None
    progress_percent: Optional[int] = None
    logs: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    class Config:
        from_attributes = True


@router.get("", response_model=List[InstallationTaskResponse])
async def get_installation_history(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get installation history for a server"""
    # Verify server exists
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    # Get all installation tasks for this server
    tasks = InstallationTaskDAO.get_by_server(db, server_id)
    
    return [
        InstallationTaskResponse(
            id=task.id,
            server_id=task.server_id,
            boot_task_id=task.boot_task_id,
            template_id=task.template_id,
            template_parameters=task.template_parameters,
            status=task.status.value,
            os_name=task.os_name,
            os_version=task.os_version,
            progress_percent=task.progress_percent,
            logs=task.logs,
            error_message=task.error_message,
            created_at=task.created_at.isoformat() if task.created_at else None,
            started_at=task.started_at.isoformat() if task.started_at else None,
            completed_at=task.completed_at.isoformat() if task.completed_at else None
        )
        for task in tasks
    ]


@router.post("/{task_id}/logs", status_code=status.HTTP_200_OK)
async def update_installation_logs(
    server_id: int,
    task_id: int,
    log_data: InstallationTaskLogUpdate,
    token: Optional[str] = Query(None, description="One-time download token"),
    db: Session = Depends(get_db)
):
    """
    Update installation task logs.
    
    This endpoint is called by the installation script to upload logs.
    Requires a valid download token from the boot task for security.
    """
    # Verify server exists
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    # Get installation task
    task = InstallationTaskDAO.get_by_id(db, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Installation task not found"
        )
    
    # Verify task belongs to this server
    if task.server_id != server_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Installation task does not belong to this server"
        )
    
    # Validate token if provided (recommended for security)
    if token:
        download_token_service = get_download_token_service()
        # Validate token - allow any filename for log uploads
        token_data = download_token_service.validate_token(token, f"logs-{task_id}")
        if not token_data or token_data.get("boot_task_id") != task.boot_task_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired download token"
            )
        # Don't mark token as used for log uploads (can be used multiple times for logs)
    
    # Update logs (replace existing logs with new ones)
    task.logs = log_data.logs
    
    # Update status if provided
    if log_data.status:
        if log_data.status == "completed":
            InstallationTaskDAO.mark_completed(db, task_id)
        elif log_data.status == "failed":
            InstallationTaskDAO.mark_failed(db, task_id, log_data.error_message)
        elif log_data.status == "in_progress":
            InstallationTaskDAO.mark_in_progress(db, task_id)
        # Status update methods already commit, so we just refresh
        db.refresh(task)
    else:
        # If no status provided, just commit the log update
        db.commit()
        db.refresh(task)
    
    logger.info(f"Updated logs for installation task {task_id} (status: {log_data.status or 'unchanged'})")
    
    return {"status": "success", "message": "Logs updated successfully"}
