from sqlalchemy.orm import Session
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from app.models.hardware_detection_report import (
    HardwareDetectionReport,
    HardwareDetectionReportStatus,
)


class HardwareDetectionReportDAO:
    """DAO for hardware detection report records."""

    @staticmethod
    def create(
        db: Session,
        server_id: int,
        created_by_user_id: Optional[int] = None,
        boot_task_id: Optional[int] = None,
        status: HardwareDetectionReportStatus = HardwareDetectionReportStatus.PENDING,
    ) -> HardwareDetectionReport:
        report = HardwareDetectionReport(
            server_id=server_id,
            boot_task_id=boot_task_id,
            created_by_user_id=created_by_user_id,
            status=status,
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        return report

    @staticmethod
    def get_by_id(db: Session, report_id: int) -> Optional[HardwareDetectionReport]:
        return db.query(HardwareDetectionReport).filter(HardwareDetectionReport.id == report_id).first()

    @staticmethod
    def get_by_boot_task_id(db: Session, boot_task_id: int) -> Optional[HardwareDetectionReport]:
        return (
            db.query(HardwareDetectionReport)
            .filter(HardwareDetectionReport.boot_task_id == boot_task_id)
            .order_by(HardwareDetectionReport.created_at.desc())
            .first()
        )

    @staticmethod
    def get_latest_for_server(db: Session, server_id: int) -> Optional[HardwareDetectionReport]:
        return (
            db.query(HardwareDetectionReport)
            .filter(HardwareDetectionReport.server_id == server_id)
            .order_by(HardwareDetectionReport.created_at.desc())
            .first()
        )

    @staticmethod
    def list_by_server(
        db: Session,
        server_id: int,
        status: Optional[HardwareDetectionReportStatus] = None,
        limit: int = 50,
    ) -> List[HardwareDetectionReport]:
        query = db.query(HardwareDetectionReport).filter(HardwareDetectionReport.server_id == server_id)
        if status:
            query = query.filter(HardwareDetectionReport.status == status)
        return query.order_by(HardwareDetectionReport.created_at.desc()).limit(limit).all()

    @staticmethod
    def mark_submitted(
        db: Session,
        report: HardwareDetectionReport,
        detected_inventory: Dict[str, Any],
        source_ip: Optional[str],
    ) -> HardwareDetectionReport:
        report.detected_inventory = detected_inventory
        report.source_ip = source_ip
        report.status = HardwareDetectionReportStatus.SUBMITTED
        report.submitted_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(report)
        return report

    @staticmethod
    def mark_rejected(
        db: Session,
        report: HardwareDetectionReport,
        reviewed_by_user_id: int,
        notes: Optional[str] = None,
    ) -> HardwareDetectionReport:
        report.status = HardwareDetectionReportStatus.REJECTED
        report.reviewed_by_user_id = reviewed_by_user_id
        report.apply_notes = notes
        report.rejected_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(report)
        return report

    @staticmethod
    def mark_applied(
        db: Session,
        report: HardwareDetectionReport,
        reviewed_by_user_id: int,
        nic_remap: Dict[str, Any],
        diff_snapshot: Dict[str, Any],
        notes: Optional[str] = None,
    ) -> HardwareDetectionReport:
        report.status = HardwareDetectionReportStatus.APPLIED
        report.reviewed_by_user_id = reviewed_by_user_id
        report.nic_remap = nic_remap
        report.diff_snapshot = diff_snapshot
        report.apply_notes = notes
        report.applied_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(report)
        return report

    @staticmethod
    def delete(db: Session, report: HardwareDetectionReport) -> None:
        """Delete a hardware detection report."""
        db.delete(report)
        db.commit()
