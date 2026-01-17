from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Enum as SQLEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base
import enum


class BootType(str, enum.Enum):
    """Boot task types"""
    LOCAL = "local"  # Boot from local disk (default)
    LINUX_SCRIPT = "linux_script"  # Boot Linux and run a script
    ISO = "iso"  # Boot from ISO image (recovery OS, live CD, etc.)
    TEMP_OS = "temp_os"  # Boot temporary OS (Alpine, SystemRescue, etc.) - uses temp_os_id
    ALPINE = "alpine"  # Boot Alpine Linux temporary OS (deprecated - use TEMP_OS instead)


class BootTaskStatus(str, enum.Enum):
    """Boot task status"""
    PENDING = "pending"  # Task is waiting to be executed
    IN_PROGRESS = "in_progress"  # Server is booting/executing
    COMPLETED = "completed"  # Task completed successfully
    FAILED = "failed"  # Task failed
    CANCELLED = "cancelled"  # Task was cancelled


class BootTask(Base):
    """
    Boot task model - represents a boot operation for a server.
    
    When a boot task exists, the PXE boot script will boot into Linux
    and execute the specified script instead of booting from local disk.
    """
    __tablename__ = "boot_tasks"

    id = Column(Integer, primary_key=True, index=True)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False, index=True)
    
    # Boot configuration
    boot_type = Column(SQLEnum(BootType, native_enum=False, values_callable=lambda x: [e.value for e in x]), nullable=False, default=BootType.LINUX_SCRIPT)
    
    # Linux boot configuration (for LINUX_SCRIPT type)
    kernel_url = Column(String(512), nullable=True)  # URL to Linux kernel (vmlinuz)
    initrd_url = Column(String(512), nullable=True)  # URL to initrd image
    kernel_params = Column(Text, nullable=True)  # Additional kernel parameters
    
    # ISO boot configuration (for ISO type)
    iso_url = Column(String(512), nullable=True)  # URL to ISO image file
    
    # Temporary OS configuration (for TEMP_OS type)
    temp_os_id = Column(String(64), nullable=True)  # ID of temporary OS (e.g., 'alpine', 'systemrescue')
    
    # Script to execute (for LINUX_SCRIPT type)
    script_url = Column(String(512), nullable=True)  # URL to script file (served by API)
    script_content = Column(Text, nullable=True)  # Script content (stored directly)
    
    # Description/notes
    description = Column(Text, nullable=True)  # Optional description of the boot task
    
    # Task status
    status = Column(SQLEnum(BootTaskStatus, native_enum=False, values_callable=lambda x: [e.value for e in x]), nullable=False, default=BootTaskStatus.PENDING)
    error_message = Column(Text, nullable=True)  # Error message if task failed
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)  # When server started booting
    completed_at = Column(DateTime(timezone=True), nullable=True)  # When task completed
    
    # Relationships
    server = relationship("Server", backref="boot_tasks")

    def __repr__(self):
        return f"<BootTask(id={self.id}, server_id={self.server_id}, boot_type={self.boot_type}, status={self.status})>"
