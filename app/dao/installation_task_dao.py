from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any
from app.models.installation_task import InstallationTask, InstallationStatus
from datetime import datetime


class InstallationTaskDAO:
    """Data Access Object for InstallationTask model"""

    @staticmethod
    def create(
        db: Session,
        server_id: int,
        boot_task_id: int,
        template_id: Optional[str] = None,
        template_parameters: Optional[Dict[str, Any]] = None,
        os_name: Optional[str] = None,
        os_version: Optional[str] = None
    ) -> InstallationTask:
        """Create a new installation task"""
        installation_task = InstallationTask(
            server_id=server_id,
            boot_task_id=boot_task_id,
            template_id=template_id,
            template_parameters=template_parameters,
            os_name=os_name,
            os_version=os_version,
            status=InstallationStatus.PENDING
        )
        db.add(installation_task)
        db.commit()
        db.refresh(installation_task)
        return installation_task

    @staticmethod
    def get_by_id(db: Session, task_id: int) -> Optional[InstallationTask]:
        """Get installation task by ID"""
        return db.query(InstallationTask).filter(InstallationTask.id == task_id).first()

    @staticmethod
    def get_by_boot_task(db: Session, boot_task_id: int) -> Optional[InstallationTask]:
        """Get installation task by boot task ID"""
        return db.query(InstallationTask).filter(InstallationTask.boot_task_id == boot_task_id).first()

    @staticmethod
    def get_by_server(db: Session, server_id: int) -> List[InstallationTask]:
        """Get all installation tasks for a server"""
        return db.query(InstallationTask).filter(InstallationTask.server_id == server_id).order_by(InstallationTask.created_at.desc()).all()

    @staticmethod
    def get_active_by_server(db: Session, server_id: int) -> Optional[InstallationTask]:
        """Get active installation task for a server (pending or in progress)"""
        return db.query(InstallationTask).filter(
            InstallationTask.server_id == server_id,
            InstallationTask.status.in_([InstallationStatus.PENDING, InstallationStatus.IN_PROGRESS])
        ).order_by(InstallationTask.created_at.desc()).first()

    @staticmethod
    def update(db: Session, installation_task: InstallationTask) -> InstallationTask:
        """Update an installation task"""
        db.commit()
        db.refresh(installation_task)
        return installation_task

    @staticmethod
    def mark_in_progress(db: Session, task_id: int) -> Optional[InstallationTask]:
        """Mark installation task as in progress"""
        task = InstallationTaskDAO.get_by_id(db, task_id)
        if task:
            task.status = InstallationStatus.IN_PROGRESS
            task.started_at = datetime.utcnow()
            db.commit()
            db.refresh(task)
        return task

    @staticmethod
    def mark_completed(db: Session, task_id: int) -> Optional[InstallationTask]:
        """Mark installation task as completed"""
        task = InstallationTaskDAO.get_by_id(db, task_id)
        if task:
            task.status = InstallationStatus.COMPLETED
            task.completed_at = datetime.utcnow()
            if task.progress_percent is None:
                task.progress_percent = 100
            db.commit()
            db.refresh(task)
        return task

    @staticmethod
    def mark_failed(db: Session, task_id: int, error_message: Optional[str] = None) -> Optional[InstallationTask]:
        """Mark installation task as failed"""
        task = InstallationTaskDAO.get_by_id(db, task_id)
        if task:
            task.status = InstallationStatus.FAILED
            task.completed_at = datetime.utcnow()
            if error_message:
                task.error_message = error_message
            db.commit()
            db.refresh(task)
        return task

    @staticmethod
    def update_progress(db: Session, task_id: int, progress_percent: int, logs: Optional[str] = None) -> Optional[InstallationTask]:
        """Update installation progress and logs"""
        task = InstallationTaskDAO.get_by_id(db, task_id)
        if task:
            task.progress_percent = max(0, min(100, progress_percent))
            if logs:
                # Append to existing logs
                if task.logs:
                    task.logs = task.logs + "\n" + logs
                else:
                    task.logs = logs
            db.commit()
            db.refresh(task)
        return task

    @staticmethod
    def update_logs(db: Session, task_id: int, logs: str) -> Optional[InstallationTask]:
        """Update installation task logs (replace existing logs)"""
        task = InstallationTaskDAO.get_by_id(db, task_id)
        if task:
            task.logs = logs
            db.commit()
            db.refresh(task)
        return task

    @staticmethod
    def cancel(db: Session, task_id: int) -> Optional[InstallationTask]:
        """Cancel an installation task"""
        task = InstallationTaskDAO.get_by_id(db, task_id)
        if task and task.status in [InstallationStatus.PENDING, InstallationStatus.IN_PROGRESS]:
            task.status = InstallationStatus.CANCELLED
            task.completed_at = datetime.utcnow()
            db.commit()
            db.refresh(task)
        return task

    @staticmethod
    def delete_pending_by_server(db: Session, server_id: int) -> int:
        """Delete all pending installation tasks for a server. Returns count deleted."""
        deleted = db.query(InstallationTask).filter(
            InstallationTask.server_id == server_id,
            InstallationTask.status == InstallationStatus.PENDING
        ).delete()
        db.commit()
        return deleted
