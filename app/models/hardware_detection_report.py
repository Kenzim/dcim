from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum as SQLEnum, JSON, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base
import enum


class HardwareDetectionReportStatus(str, enum.Enum):
    """Lifecycle status for hardware detection reports."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    APPLIED = "applied"
    REJECTED = "rejected"


class HardwareDetectionReport(Base):
    """Snapshot of detected hardware before admin approval."""

    __tablename__ = "hardware_detection_reports"

    id = Column(Integer, primary_key=True, index=True)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False, index=True)
    boot_task_id = Column(Integer, ForeignKey("boot_tasks.id"), nullable=True, index=True)
    status = Column(
        SQLEnum(
            HardwareDetectionReportStatus,
            native_enum=False,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=HardwareDetectionReportStatus.PENDING,
    )
    source_ip = Column(String(45), nullable=True)
    detected_inventory = Column(JSON, nullable=True)
    diff_snapshot = Column(JSON, nullable=True)
    nic_remap = Column(JSON, nullable=True)
    apply_notes = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reviewed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    rejected_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    server = relationship("Server", backref="hardware_detection_reports")
    boot_task = relationship("BootTask", backref="hardware_detection_reports")
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
    reviewed_by_user = relationship("User", foreign_keys=[reviewed_by_user_id])

    def __repr__(self):
        return f"<HardwareDetectionReport(id={self.id}, server_id={self.server_id}, status='{self.status.value}')>"
