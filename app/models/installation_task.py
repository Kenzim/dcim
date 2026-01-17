from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Enum as SQLEnum, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base
import enum


class InstallationStatus(str, enum.Enum):
    """Installation task status"""
    PENDING = "pending"  # Installation task created, waiting for boot task
    IN_PROGRESS = "in_progress"  # Installation in progress
    COMPLETED = "completed"  # Installation completed successfully
    FAILED = "failed"  # Installation failed
    CANCELLED = "cancelled"  # Installation was cancelled


class InstallationTask(Base):
    """
    Installation task model - tracks OS installation operations.
    
    This is separate from BootTask to allow:
    - Installation-specific tracking (logs, progress, template info)
    - Boot tasks for non-installation purposes (recovery ISOs, etc.)
    - Multiple boot tasks per installation if needed
    """
    __tablename__ = "installation_tasks"

    id = Column(Integer, primary_key=True, index=True)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False, index=True)
    boot_task_id = Column(Integer, ForeignKey("boot_tasks.id"), nullable=False, index=True)
    
    # Template information
    template_id = Column(String(255), nullable=True)  # Template used for installation
    template_parameters = Column(JSON, nullable=True)  # Parameters passed to template
    
    # Installation status
    status = Column(SQLEnum(InstallationStatus, native_enum=False, values_callable=lambda x: [e.value for e in x]), nullable=False, default=InstallationStatus.PENDING)
    
    # Installation details
    os_name = Column(String(255), nullable=True)  # OS being installed (e.g., "Windows Server 2022")
    os_version = Column(String(255), nullable=True)  # OS version
    progress_percent = Column(Integer, nullable=True)  # Installation progress (0-100)
    
    # Logs and messages
    logs = Column(Text, nullable=True)  # Installation logs/output
    error_message = Column(Text, nullable=True)  # Error message if installation failed
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)  # When installation started
    completed_at = Column(DateTime(timezone=True), nullable=True)  # When installation completed
    
    # Relationships
    server = relationship("Server", backref="installation_tasks")
    boot_task = relationship("BootTask", backref="installation_task")

    def __repr__(self):
        return f"<InstallationTask(id={self.id}, server_id={self.server_id}, template_id={self.template_id}, status={self.status})>"
