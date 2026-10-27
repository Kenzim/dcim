import secrets
from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.boot_task import BootTask, BootTaskStatus, BootType


class BootTaskDAO:
    """Data Access Object for BootTask model"""

    @staticmethod
    def create(
        db: Session,
        server_id: int,
        boot_type: str = "linux_script",
        kernel_url: Optional[str] = None,
        initrd_url: Optional[str] = None,
        kernel_params: Optional[str] = None,
        script_url: Optional[str] = None,
        script_content: Optional[str] = None,
        iso_url: Optional[str] = None,
        temp_os_id: Optional[str] = None,
        description: Optional[str] = None
    ) -> BootTask:
        """Create a new boot task"""
        # Cancel any existing pending tasks for this server
        BootTaskDAO.cancel_pending_tasks(db, server_id)
        
        # Convert boot_type string to enum if needed
        if isinstance(boot_type, str):
            try:
                boot_type_enum = BootType(boot_type.lower())
            except ValueError:
                # Fallback to default if invalid
                boot_type_enum = BootType.LINUX_SCRIPT
        else:
            boot_type_enum = boot_type
        
        boot_task = BootTask(
            server_id=server_id,
            boot_type=boot_type_enum,
            kernel_url=kernel_url,
            initrd_url=initrd_url,
            kernel_params=kernel_params,
            script_url=script_url,
            script_content=script_content,
            iso_url=iso_url,
            temp_os_id=temp_os_id,
            description=description,
            status=BootTaskStatus.PENDING,
            pxe_nonce=secrets.token_urlsafe(24),
        )
        db.add(boot_task)
        db.commit()
        db.refresh(boot_task)
        BootTaskDAO._schedule_dhcp_sync(server_id)
        return boot_task

    @staticmethod
    def get_by_id(db: Session, task_id: int) -> Optional[BootTask]:
        """Get boot task by ID"""
        return db.query(BootTask).filter(BootTask.id == task_id).first()

    @staticmethod
    def get_pending_by_server(db: Session, server_id: int) -> Optional[BootTask]:
        """Get pending boot task for a server"""
        return db.query(BootTask).filter(
            BootTask.server_id == server_id,
            BootTask.status == BootTaskStatus.PENDING
        ).first()

    @staticmethod
    def get_active_by_server(db: Session, server_id: int) -> Optional[BootTask]:
        """Get active (pending or in_progress) boot task for a server"""
        return db.query(BootTask).filter(
            BootTask.server_id == server_id,
            BootTask.status.in_([BootTaskStatus.PENDING, BootTaskStatus.IN_PROGRESS])
        ).order_by(BootTask.created_at.desc()).first()

    @staticmethod
    def get_all_by_server(db: Session, server_id: int) -> List[BootTask]:
        """Get all boot tasks for a server"""
        return db.query(BootTask).filter(
            BootTask.server_id == server_id
        ).order_by(BootTask.created_at.desc()).all()

    @staticmethod
    def cancel_pending_tasks(db: Session, server_id: int) -> int:
        """Cancel all pending tasks for a server"""
        count = db.query(BootTask).filter(
            BootTask.server_id == server_id,
            BootTask.status == BootTaskStatus.PENDING
        ).update({"status": BootTaskStatus.CANCELLED})
        db.commit()
        return count

    @staticmethod
    def cancel_open_tasks(db: Session, server_id: int) -> int:
        """Cancel all pending and in-progress tasks for a server"""
        count = db.query(BootTask).filter(
            BootTask.server_id == server_id,
            BootTask.status.in_([BootTaskStatus.PENDING, BootTaskStatus.IN_PROGRESS])
        ).update({"status": BootTaskStatus.CANCELLED}, synchronize_session=False)
        db.commit()
        return count

    @staticmethod
    def _schedule_dhcp_sync(server_id: int) -> None:
        """dhcpd only advertises PXE options for pending tasks; refresh it after changes."""
        from app.services.dhcp_sync import schedule_dhcp_sync
        schedule_dhcp_sync(server_id)

    @staticmethod
    def consume_pxe_nonce(db: Session, task_id: int) -> bool:
        """Atomically mark a task's PXE nonce as used. Returns False if it was already used."""
        from datetime import datetime, timezone
        count = db.query(BootTask).filter(
            BootTask.id == task_id,
            BootTask.pxe_nonce.isnot(None),
            BootTask.pxe_nonce_used_at.is_(None),
        ).update({"pxe_nonce_used_at": datetime.now(timezone.utc)}, synchronize_session="fetch")
        db.commit()
        return count == 1

    @staticmethod
    def mark_in_progress(db: Session, task_id: int) -> Optional[BootTask]:
        """Mark a boot task as in progress"""
        from datetime import datetime, timezone
        task = BootTaskDAO.get_by_id(db, task_id)
        if task:
            task.status = BootTaskStatus.IN_PROGRESS
            task.started_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(task)
        return task

    @staticmethod
    def mark_completed(db: Session, task_id: int) -> Optional[BootTask]:
        """Mark a boot task as completed"""
        from datetime import datetime, timezone
        task = BootTaskDAO.get_by_id(db, task_id)
        if task:
            task.status = BootTaskStatus.COMPLETED
            task.completed_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(task)
        return task

    @staticmethod
    def mark_failed(db: Session, task_id: int, error_message: Optional[str] = None) -> Optional[BootTask]:
        """Mark a boot task as failed"""
        from datetime import datetime, timezone
        task = BootTaskDAO.get_by_id(db, task_id)
        if task:
            task.status = BootTaskStatus.FAILED
            task.error_message = error_message
            task.completed_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(task)
        return task

    @staticmethod
    def delete(db: Session, task_id: int) -> bool:
        """Delete a boot task"""
        task = BootTaskDAO.get_by_id(db, task_id)
        if task:
            db.delete(task)
            db.commit()
            return True
        return False
