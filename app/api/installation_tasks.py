"""
API endpoints for installation task management
"""
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.services.download_token_service import get_download_token_service
from app.services.os_template_service import get_template_service
from app.dao.installation_task_dao import InstallationTaskDAO
from app.dao.boot_task_dao import BootTaskDAO
from app.models.installation_task import InstallationTask, InstallationStatus
from app.models.server_activity import ServerActivityEventType
from app.dao.server_dao import ServerDAO
from app.services.server_activity_logger import log_server_activity_success, log_server_activity_failure
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/servers/{server_id}/installation-tasks", tags=["installation-tasks"])

# Path to shared post-install boot-order finalize script (works in dev and Docker)
_FINALIZE_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "finalize-pxe-bootorder.sh"


def _schedule_post_install_boot_order_fix(db: Session, task: InstallationTask) -> None:
    """
    If the installation task's template has post_install_fix_boot_order enabled,
    create a one-time follow-up BootTask (debian-live + finalize script) so the
    next PXE boot runs the boot-order fix and then reboots into the installed OS.
    """
    if not task.template_id:
        return
    template_service = get_template_service()
    template = template_service.get_template(task.template_id)
    if not template or not getattr(template, "post_install_fix_boot_order", False):
        return
    if not _FINALIZE_SCRIPT_PATH.exists():
        logger.warning("Post-install boot order fix requested but script not found: %s", _FINALIZE_SCRIPT_PATH)
        return
    try:
        script_content = _FINALIZE_SCRIPT_PATH.read_text()
    except Exception as e:
        logger.warning("Failed to read finalize script %s: %s", _FINALIZE_SCRIPT_PATH, e)
        return
    script_content = 'export AUTO_REBOOT=0\n' + script_content
    if template.efi_boot_loader:
        # Inject so the script can set BootNext to the installed OS entry
        script_content = f'export EFI_BOOT_LOADER="{template.efi_boot_loader}"\n' + script_content
    BootTaskDAO.create(
        db,
        server_id=task.server_id,
        boot_type="temp_os",
        temp_os_id="debian-live",
        script_content=script_content,
        description="Post-install boot order fix",
    )
    logger.info(
        "Scheduled post-install boot order fix for server %s (template %s)",
        task.server_id,
        task.template_id,
    )


class InstallationTaskLogUpdate(BaseModel):
    """Request model for updating installation task logs"""
    logs: str
    status: Optional[str] = None  # "completed" or "failed"
    error_message: Optional[str] = None


class InstallationTaskStatusUpdate(BaseModel):
    """Request model for manually updating installation task status (admin)"""
    status: str  # "in_progress" | "completed" | "failed" | "cancelled"
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


@router.post("/purge-pending")
async def purge_pending_installation_tasks(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete all pending installation tasks for this server (admin only)."""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    deleted = InstallationTaskDAO.delete_pending_by_server(db, server_id)
    logger.info(f"Purged {deleted} pending installation task(s) for server {server_id}")
    return {"deleted": deleted}


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
    
    # Update logs (replace existing logs with new ones) and persist first
    task.logs = log_data.logs
    db.commit()

    # Update status if provided (mark_* refetch task and commit; logs already saved above)
    if log_data.status:
        if log_data.status == "completed":
            task = InstallationTaskDAO.mark_completed(db, task_id)
            log_server_activity_success(
                db,
                server_id=server_id,
                event_type=ServerActivityEventType.INSTALL,
                action="installation_completed",
                source="installer",
                message=f"Installation task {task_id} completed",
                details={"installation_task_id": task_id, "boot_task_id": task.boot_task_id},
            )
            if task:
                _schedule_post_install_boot_order_fix(db, task)
        elif log_data.status == "failed":
            InstallationTaskDAO.mark_failed(db, task_id, log_data.error_message)
            log_server_activity_failure(
                db,
                server_id=server_id,
                event_type=ServerActivityEventType.INSTALL,
                action="installation_failed",
                source="installer",
                message=f"Installation task {task_id} failed",
                details={
                    "installation_task_id": task_id,
                    "boot_task_id": task.boot_task_id,
                },
                error=Exception(log_data.error_message or "Installation failed"),
            )
        elif log_data.status == "in_progress":
            InstallationTaskDAO.mark_in_progress(db, task_id)
            log_server_activity_success(
                db,
                server_id=server_id,
                event_type=ServerActivityEventType.INSTALL,
                action="installation_in_progress",
                source="installer",
                message=f"Installation task {task_id} started",
                details={"installation_task_id": task_id, "boot_task_id": task.boot_task_id},
            )
    db.refresh(task)
    
    logger.info(f"Updated logs for installation task {task_id} (status: {log_data.status or 'unchanged'})")
    
    return {"status": "success", "message": "Logs updated successfully"}


@router.patch("/{task_id}", response_model=InstallationTaskResponse)
async def update_installation_task_status(
    server_id: int,
    task_id: int,
    body: InstallationTaskStatusUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Manually update installation task status (admin only).
    Use to mark pending/stuck tasks as completed, failed, or cancelled.
    """
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    task = InstallationTaskDAO.get_by_id(db, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Installation task not found"
        )
    if task.server_id != server_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Installation task does not belong to this server"
        )
    status_val = (body.status or "").strip().lower()
    if status_val not in ("in_progress", "completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="status must be one of: in_progress, completed, failed, cancelled"
        )
    if status_val == "in_progress":
        task = InstallationTaskDAO.mark_in_progress(db, task_id)
        activity_action = "installation_in_progress"
    elif status_val == "completed":
        task = InstallationTaskDAO.mark_completed(db, task_id)
        activity_action = "installation_completed"
        if task:
            _schedule_post_install_boot_order_fix(db, task)
    elif status_val == "failed":
        task = InstallationTaskDAO.mark_failed(db, task_id, body.error_message)
        activity_action = "installation_failed"
    else:
        task = InstallationTaskDAO.cancel(db, task_id)
        activity_action = "installation_cancelled"
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Installation task not found"
        )
    if status_val == "failed":
        log_server_activity_failure(
            db,
            server_id=server_id,
            event_type=ServerActivityEventType.INSTALL,
            action=activity_action,
            source="admin_api",
            message=f"Installation task {task_id} status set to failed",
            details={
                "installation_task_id": task_id,
                "boot_task_id": task.boot_task_id,
            },
            error=Exception(body.error_message or "Marked as failed by admin"),
        )
    else:
        log_server_activity_success(
            db,
            server_id=server_id,
            event_type=ServerActivityEventType.INSTALL,
            action=activity_action,
            source="admin_api",
            message=f"Installation task {task_id} status set to {status_val}",
            details={
                "installation_task_id": task_id,
                "boot_task_id": task.boot_task_id,
                "error_message": body.error_message,
            },
        )
    return InstallationTaskResponse(
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
